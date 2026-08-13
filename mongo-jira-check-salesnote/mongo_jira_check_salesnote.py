#!/usr/bin/env python3
"""Daily check: open Jira "Sale Note" reconcile tickets (project SUP, label
PS_Front) — look up the docNo (e.g. SN26-TH004759-M02-0000073) from the
ticket against MongoDB `store.sale_notes`, and move the ticket according to
its status:
  - status = COMPLETED  -> close the ticket (matches how the team already
    closes these tickets by hand)
  - status = NEW        -> flag the ticket and make sure it's in "In
    Progress" (still needs the doc synced from POS/HH)
(Companion to mongo-jira-check-pointsum — same idea, different collection —
and to gcp-jira-check-rsp, whose Open -> In Progress -> flag/Close movement this
reuses exactly.)

Runs on ANY teammate's machine. Each person needs their own:
  1. `pip3 install pymongo`
  2. A Jira Personal Access Token, supplied via ONE of (checked in order):
       a. env vars JIRA_URL / JIRA_PERSONAL_TOKEN
       b. a config file at ~/.mongo_jira_check.json:
            {"jira_url": "https://jira.tdshop.io/", "jira_token": "..."}
       c. a config file at ~/.rsp_sync_check.json (same shape) — reused if
          you already set that up for the RSP Sync Check tool
       d. (fallback, for Claude Code users) ~/.claude.json's
          mcpServers.mcp-atlassian.env block
  3. A MongoDB connection string with read access to the `store` DB,
     supplied via ONE of (checked in order):
       a. env var MONGO_URI
       b. ~/.mongo_jira_check.json's "mongo_uri" field — reused if you
          already set this up for the Point Sum Check tool
     Use the read-only PROD connection string documented in Confluence
     ("Tooling Onboarding Checklist", space TOOK) — the `support_read_only`
     account. Never commit this string anywhere; it belongs only in your
     local config file / env var.

Per ticket (doc_no in the description = sale note docNo):
  - run the same aggregate the team uses to check sale notes by hand against
    MongoDB `store.sale_notes`
  - the ticket is assigned to whoever's token ran the check, but ONLY if it
    doesn't already have an assignee (never overwrites an existing one)
  - if status is "Open" it always transitions to "In Progress" first
    (whether or not the sale note is already COMPLETED — a ticket found
    already COMPLETED while still Open passes straight through to Close
    below in the same run, it doesn't get stuck waiting on a separate pass)
  - once in "In Progress": if the sale note is still NEW, adds the
    "Impediment" flag; if it's COMPLETED, removes the flag (never left on
    when closing) and transitions to "Close" with resolution="Won't Do" and
    Fix Version/s="Won't Fix Release"
  - if the docNo isn't found in Mongo at all: leave the ticket untouched and
    flag it for a human to check the docNo
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_VERSION = "1.0.1"
VERSION_URL = (
    "https://raw.githubusercontent.com/natingkaninantanaxyt-web/"
    "front-automation-hub/main/mongo-jira-check-salesnote/VERSION"
)

JQL = (
    'project = SUP AND labels = PS_Front AND summary ~ "SalesNote" '
    "AND statusCategory != Done ORDER BY created ASC"
)
MARKER = "Auto Sale Note Check"
LOCAL_CONFIG_PATH = Path.home() / ".mongo_jira_check.json"
RSP_CONFIG_PATH = Path.home() / ".rsp_sync_check.json"

DB_NAME = "store"
COLLECTION_NAME = "sale_notes"

STATUS_COMPLETED = "COMPLETED"
FLAG_VALUE = "Impediment"
RESOLUTION_WONT_DO = "Won't Do"
FIX_VERSION_WONT_FIX = "Won't Fix Release"
TRANSITION_IN_PROGRESS = "In Progress"
TRANSITION_CLOSE = "Close"

DRY_RUN = "--dry-run" in sys.argv
SKIP_UPDATE_CHECK = "--skip-update-check" in sys.argv

DOC_NO_RE = re.compile(r"\|\|\s*doc_no\s*\|\s*([^\|]+?)\s*\|")


def fail(message):
    print(f"\n✖ {message}\n", file=sys.stderr)
    sys.exit(1)


def check_for_updates():
    if SKIP_UPDATE_CHECK:
        return
    try:
        proc = subprocess.run(
            ["curl", "-fsS", "--max-time", "5", VERSION_URL],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return  # offline / blocked — don't block the run over this
        latest = proc.stdout.strip()
        if latest and latest != SCRIPT_VERSION:
            print("=" * 70)
            print(f"⚠ This script is OUTDATED (you have v{SCRIPT_VERSION}, latest is v{latest}).")
            print("  Download the newest copy before continuing — results/behavior may be")
            print("  wrong or incomplete on this old version.")
            print("=" * 70)
            answer = input("Continue anyway with the outdated version? [y/N]: ").strip().lower()
            if answer != "y":
                sys.exit(1)
            print()
    except Exception:
        return  # best-effort only, never crash the run over the update check


def _read_json(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def load_jira_creds():
    url = os.environ.get("JIRA_URL")
    token = os.environ.get("JIRA_PERSONAL_TOKEN")
    if url and token:
        return url.rstrip("/"), token, "environment variables"

    for path in (LOCAL_CONFIG_PATH, RSP_CONFIG_PATH):
        cfg = _read_json(path)
        url, token = cfg.get("jira_url"), cfg.get("jira_token")
        if url and token:
            return url.rstrip("/"), token, str(path)

    claude_cfg_path = Path.home() / ".claude.json"
    cfg = _read_json(claude_cfg_path)
    env = cfg.get("mcpServers", {}).get("mcp-atlassian", {}).get("env", {})
    url, token = env.get("JIRA_URL"), env.get("JIRA_PERSONAL_TOKEN")
    if url and token:
        return url.rstrip("/"), token, "~/.claude.json (mcp-atlassian)"

    fail(
        "Jira credentials not found. Set them up with ONE of these options:\n\n"
        "  Option A - environment variables (this terminal session only):\n"
        '    export JIRA_URL="https://jira.tdshop.io/"\n'
        '    export JIRA_PERSONAL_TOKEN="<your Personal Access Token>"\n\n'
        f"  Option B - config file (persists across sessions), create {LOCAL_CONFIG_PATH} with:\n"
        '    {"jira_url": "https://jira.tdshop.io/", "jira_token": "<your Personal Access Token>"}\n\n'
        "  Get a Personal Access Token from Jira: avatar (top right) -> Profile -> "
        "Personal Access Tokens -> Create token"
    )


def load_mongo_uri():
    uri = os.environ.get("MONGO_URI")
    if uri:
        return uri, "environment variable"

    cfg = _read_json(LOCAL_CONFIG_PATH)
    uri = cfg.get("mongo_uri")
    if uri:
        return uri, str(LOCAL_CONFIG_PATH)

    fail(
        "MongoDB connection string not found. Set it up with ONE of these options:\n\n"
        "  Option A - environment variable (this terminal session only):\n"
        '    export MONGO_URI="mongodb://support_read_only:<password>@..."\n\n'
        f"  Option B - config file (persists across sessions), add to {LOCAL_CONFIG_PATH}:\n"
        '    {"mongo_uri": "mongodb://support_read_only:<password>@..."}\n\n'
        "  Get the read-only PROD connection string from Confluence: "
        '"Tooling Onboarding Checklist" (space TOOK) -> "Setup MongoDB Connection to '
        'PROD and NEST BETA" -> PROD (Local). Replace {UserName} in appName with your '
        "own name, and never commit this string anywhere."
    )


def check_pymongo_ready():
    try:
        import pymongo  # noqa: F401
        return pymongo
    except ImportError:
        fail("`pymongo` not installed on this machine.\n  Run: pip3 install pymongo")


def jira_request(base_url, token, path, method="GET", body=None):
    # Uses curl (system trust store) instead of urllib, whose bundled
    # certifi CA list may not include this org's internal CA. The token is
    # passed via a curl -K config file (mode 600), not argv, so it never
    # shows up in `ps`.
    url = f"{base_url}{path}"
    with tempfile.NamedTemporaryFile("w", suffix=".curlcfg", delete=False) as cfg:
        cfg.write(f'header = "Authorization: Bearer {token}"\n')
        cfg.write('header = "Content-Type: application/json"\n')
        cfg_path = cfg.name
    Path(cfg_path).chmod(0o600)
    try:
        cmd = ["curl", "-sS", "-K", cfg_path, "-X", method, "-w", "\n%{http_code}", url]
        if body is not None:
            cmd += ["--data-binary", json.dumps(body)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"curl failed ({proc.returncode}): {proc.stderr.strip()}")
        body_text, _, status_code = proc.stdout.rpartition("\n")
        if int(status_code) >= 400:
            raise RuntimeError(f"Jira API returned HTTP {status_code} for {method} {path}: {body_text.strip()[:500]}")
        return json.loads(body_text) if body_text.strip() else {}
    finally:
        Path(cfg_path).unlink(missing_ok=True)


def get_current_user(base_url, token):
    me = jira_request(base_url, token, "/rest/api/2/myself")
    return me.get("name"), me.get("displayName")


def set_assignee(base_url, token, issue_key, username):
    jira_request(base_url, token, f"/rest/api/2/issue/{issue_key}/assignee", "PUT", {"name": username})


def set_flag(base_url, token, issue_key, flagged):
    # The "Flagged" field can't be set through the normal issue-edit API —
    # Jira rejects it with "not on the appropriate screen" (HTTP 400) since
    # it's only exposed via the board's flag action, not any edit screen.
    # This is that action's actual (undocumented but stable) endpoint.
    jira_request(
        base_url, token, "/rest/greenhopper/1.0/xboard/issue/flag/flag.json", "POST",
        {"issueKeys": [issue_key], "flag": flagged},
    )


def get_transition_id(base_url, token, issue_key, transition_name):
    result = jira_request(base_url, token, f"/rest/api/2/issue/{issue_key}/transitions")
    for t in result.get("transitions", []):
        if t["name"] == transition_name:
            return t["id"]
    return None


def transition_issue(base_url, token, issue_key, transition_id, fields=None):
    body = {"transition": {"id": transition_id}}
    if fields:
        body["fields"] = fields
    jira_request(base_url, token, f"/rest/api/2/issue/{issue_key}/transitions", "POST", body)


def post_comment(base_url, token, issue_key, body):
    jira_request(base_url, token, f"/rest/api/2/issue/{issue_key}/comment", "POST", {"body": body})


def search_open_sale_note_tickets(base_url, token):
    body = {
        "jql": JQL,
        "fields": ["summary", "description", "status", "assignee"],
        "maxResults": 50,
    }
    result = jira_request(base_url, token, "/rest/api/2/search", "POST", body)
    return result.get("issues", [])


def parse_ticket(issue):
    desc = issue["fields"]["description"] or ""
    doc_no_m = DOC_NO_RE.search(desc)
    if not doc_no_m:
        return None
    return {
        "key": issue["key"],
        "summary": issue["fields"]["summary"],
        "doc_no": doc_no_m.group(1),
    }


def query_sale_note(collection, doc_no):
    # Same aggregate the team already uses to check sale notes by hand.
    pipeline = [
        {"$match": {"docNo": doc_no}},
        {
            "$addFields": {
                "solution": {
                    "$cond": {
                        "if": {"$eq": ["$status", "NEW"]}, "then": "Please Sync POS/HH",
                        "else": "OK",
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "saleNoteNo": 1,
                "docNo": 1,
                "status": 1,
                "storeCode": 1,
                "receiptNo": {"$cond": {"if": {"$gt": [{"$ifNull": ["$receiptNo", ""]}, ""]}, "then": "$receiptNo", "else": "-"}},
                "saleNoteDate": {"$toDate": {"$multiply": ["$createdDate._seconds", 1000]}},
                "solution": 1,
            }
        },
    ]
    result = list(collection.aggregate(pipeline, maxTimeMS=20000))
    return result[0] if result else None


def already_reported(base_url, token, issue_key, note):
    comments = jira_request(
        base_url, token, f"/rest/api/2/issue/{issue_key}/comment?orderBy=-created&maxResults=1"
    ).get("comments", [])
    if not comments:
        return False
    last_body = comments[0].get("body", "")
    if MARKER not in last_body:
        return False
    return f"status: {note['status']}" in last_body


def format_comment(ticket, note):
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %z")
    lines = [
        f"*{MARKER}* ({now})",
        f"docNo: {ticket['doc_no']}",
        f"saleNoteNo: {note['saleNoteNo']}",
        f"status: {note['status']}",
        f"storeCode: {note['storeCode']}",
        f"receiptNo: {note['receiptNo']}",
        f"saleNoteDate: {note['saleNoteDate']}",
    ]
    if note["status"] == STATUS_COMPLETED:
        lines.append("ข้อมูล sync แล้วครับ ปิด ticket อัตโนมัติ")
    else:
        lines.append(f"solution: {note['solution']}")
    return "\n".join(lines)


def main():
    print(f"{Path(__file__).name} v{SCRIPT_VERSION}\n")
    check_for_updates()

    pymongo = check_pymongo_ready()
    base_url, token, jira_creds_source = load_jira_creds()
    mongo_uri, mongo_creds_source = load_mongo_uri()
    username, display_name = get_current_user(base_url, token)

    print(f"Jira credentials from: {jira_creds_source}")
    print(f"Mongo URI from: {mongo_creds_source}")
    print(f"Running as: {display_name} ({username})\n")

    mongo_client = pymongo.MongoClient(mongo_uri, serverSelectionTimeoutMS=10000)
    try:
        mongo_client.admin.command("ping")
    except Exception as e:
        fail(f"Could not connect to MongoDB: {e}")
    collection = mongo_client[DB_NAME][COLLECTION_NAME]

    issues = search_open_sale_note_tickets(base_url, token)
    if not issues:
        print("No open Sale Note tickets found.")
        return

    for issue in issues:
        ticket = parse_ticket(issue)
        if not ticket:
            print(f"[{issue['key']}] could not parse doc_no from description, skipping")
            continue

        note = query_sale_note(collection, ticket["doc_no"])
        if note is None:
            print(f"[{ticket['key']}] docNo={ticket['doc_no']}: no sale_notes record found in Mongo, skipping")
            continue

        needs_work = note["status"] != STATUS_COMPLETED
        current_status = issue["fields"]["status"]["name"]
        current_assignee = issue["fields"].get("assignee")

        print(f"[{ticket['key']}] {ticket['summary']} (status={current_status})")
        print(f"  docNo={ticket['doc_no']} saleNoteStatus={note['status']} saleNoteNo={note['saleNoteNo']} storeCode={note['storeCode']}")

        comment = format_comment(ticket, note)

        if DRY_RUN:
            if current_assignee:
                print(f"  assignee already set ({current_assignee.get('displayName')}), would leave unchanged")
            else:
                print(f"  --- would assign to {display_name} ({username}) ---")
            print("  --- would post comment ---")
            print("  " + comment.replace("\n", "\n  "))
            preview_status = current_status
            if preview_status == "Open":
                print(f"  --- would transition Open -> {TRANSITION_IN_PROGRESS} ---")
                preview_status = TRANSITION_IN_PROGRESS
            if preview_status == TRANSITION_IN_PROGRESS:
                if needs_work:
                    print(f"  --- would (re)flag as '{FLAG_VALUE}' ---")
                else:
                    print(
                        f"  --- would unflag, transition -> {TRANSITION_CLOSE} "
                        f"(resolution='{RESOLUTION_WONT_DO}', fixVersion='{FIX_VERSION_WONT_FIX}') ---"
                    )
            continue

        if current_assignee:
            print(f"  assignee already set ({current_assignee.get('displayName')}), leaving unchanged")
        else:
            set_assignee(base_url, token, ticket["key"], username)
            print(f"  assigned to {display_name} ({username})")

        if already_reported(base_url, token, ticket["key"], note):
            print("  unchanged since last comment, skipping post")
        else:
            post_comment(base_url, token, ticket["key"], comment)
            print("  comment posted")

        if current_status == "Open":
            tid = get_transition_id(base_url, token, ticket["key"], TRANSITION_IN_PROGRESS)
            if tid:
                transition_issue(base_url, token, ticket["key"], tid)
                current_status = TRANSITION_IN_PROGRESS
                print(f"  status: Open -> {TRANSITION_IN_PROGRESS}")
            else:
                print(f"  WARNING: '{TRANSITION_IN_PROGRESS}' transition not available, status unchanged")

        # Re-checked every run (not just on the Open -> In Progress edge above) so a
        # ticket that was already In Progress but never got flagged gets caught up here.
        if current_status == TRANSITION_IN_PROGRESS:
            if needs_work:
                set_flag(base_url, token, ticket["key"], True)
                print(f"  flagged as '{FLAG_VALUE}'")
            else:
                tid = get_transition_id(base_url, token, ticket["key"], TRANSITION_CLOSE)
                if tid:
                    set_flag(base_url, token, ticket["key"], False)
                    transition_issue(
                        base_url, token, ticket["key"], tid,
                        fields={
                            "resolution": {"name": RESOLUTION_WONT_DO},
                            "fixVersions": [{"name": FIX_VERSION_WONT_FIX}],
                        },
                    )
                    print(
                        f"  status: In Progress -> {TRANSITION_CLOSE}, unflagged, "
                        f"resolution='{RESOLUTION_WONT_DO}', fixVersion='{FIX_VERSION_WONT_FIX}'"
                    )
                else:
                    print(f"  WARNING: '{TRANSITION_CLOSE}' transition not available, status unchanged")


if __name__ == "__main__":
    main()
