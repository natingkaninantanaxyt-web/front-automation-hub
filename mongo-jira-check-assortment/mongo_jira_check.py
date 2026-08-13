#!/usr/bin/env python3
"""Daily check: open Jira "POS Assortment" reconcile tickets (project SUP,
label PS_Front) — a ticket lists one barcode + one or more store codes that
already had a POS Assortment repair job submitted (Jenkins link in the
description). This script re-runs the team's manual MongoDB check for every
store code on the ticket (store code -> store no in `store.stores`, then
look up `store.pos_assortments` by storeNo+barcode) and moves the ticket
according to whether ALL of its stores are now synced.
(Companion to mongo-jira-check-salesnote / mongo-jira-check-pointsum — same
idea, different collection — and to rsp-sync-check, whose Open -> In
Progress -> flag/Close movement this reuses exactly.)

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
          already set this up for the Point Sum / Sale Note Check tools
     Use the read-only PROD connection string documented in Confluence
     ("Tooling Onboarding Checklist", space TOOK) — the `support_read_only`
     account. Never commit this string anywhere; it belongs only in your
     local config file / env var.

Per ticket (Barcode + Store Codes parsed from the description table):
  - for every store code: look up its store no (`store.stores`, matched by
    `code`), then look up `store.pos_assortments` by storeNo+barcode — no
    match means that store's assortment hasn't synced yet
  - the ticket is assigned to whoever's token ran the check, but ONLY if it
    doesn't already have an assignee (never overwrites an existing one)
  - if status is "Open" it always transitions to "In Progress" first
  - once in "In Progress": if EVERY store code is synced, removes the
    "Impediment" flag (never left on when closing) and transitions to
    "Close" with resolution="Won't Do" and Fix Version/s="Won't Fix
    Release"; if ANY store is still unsynced (or a store code couldn't be
    found in `store.stores` at all), adds/keeps the "Impediment" flag so a
    human re-runs the POS Repair Jenkins job already linked on the ticket
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_VERSION = "1.0.0"
VERSION_URL = (
    "https://raw.githubusercontent.com/natingkaninantanaxyt-web/"
    "front-automation-hub/main/mongo-jira-check-assortment/VERSION"
)

JQL = (
    'project = SUP AND labels = PS_Front AND summary ~ "\\"POS Assortment\\"" '
    "AND statusCategory != Done ORDER BY created ASC"
)
MARKER = "Auto POS Assortment Check"
LOCAL_CONFIG_PATH = Path.home() / ".mongo_jira_check.json"
RSP_CONFIG_PATH = Path.home() / ".rsp_sync_check.json"

DB_NAME = "store"
STORES_COLLECTION = "stores"
ASSORTMENT_COLLECTION = "pos_assortments"

STATUS_SYNCED = "synced"
STATUS_NOT_SYNCED = "not_synced"
STATUS_STORE_NOT_FOUND = "store_not_found"

FLAG_VALUE = "Impediment"
RESOLUTION_WONT_DO = "Won't Do"
FIX_VERSION_WONT_FIX = "Won't Fix Release"
TRANSITION_IN_PROGRESS = "In Progress"
TRANSITION_CLOSE = "Close"

DRY_RUN = "--dry-run" in sys.argv
SKIP_UPDATE_CHECK = "--skip-update-check" in sys.argv

BARCODE_RE = re.compile(r"\|\s*Barcode\s*\|\s*([^\|\n]+?)\s*\|")
STORE_CODES_RE = re.compile(r"\|\s*Store Codes\s*\|(.*?)\|\s*\n\|---\|---\|", re.DOTALL)


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


def search_open_assortment_tickets(base_url, token):
    body = {
        "jql": JQL,
        "fields": ["summary", "description", "status", "assignee"],
        "maxResults": 50,
    }
    result = jira_request(base_url, token, "/rest/api/2/search", "POST", body)
    return result.get("issues", [])


def parse_store_codes(raw):
    text = raw.replace("```", "")
    parts = re.split(r"[\s,]+", text.strip())
    return [p for p in parts if p]


def parse_ticket(issue):
    desc = issue["fields"]["description"] or ""
    barcode_m = BARCODE_RE.search(desc)
    store_codes_m = STORE_CODES_RE.search(desc)
    if not barcode_m or not store_codes_m:
        return None
    store_codes = parse_store_codes(store_codes_m.group(1))
    if not store_codes:
        return None
    return {
        "key": issue["key"],
        "summary": issue["fields"]["summary"],
        "barcode": barcode_m.group(1),
        "store_codes": store_codes,
    }


def query_store_no(stores_col, store_code):
    doc = stores_col.find_one(
        {"code": {"$in": [store_code]}},
        {"_id": 0, "no": 1},
        sort=[("_id", -1)],
    )
    return doc.get("no") if doc else None


def query_assortment(assortment_col, store_no, barcode):
    return assortment_col.find_one(
        {"storeNo": store_no, "barcode": barcode},
        {
            "_id": 0, "key": 1, "storeNo": 1, "createdDate": 1,
            "lastModifiedDate": 1, "lastSyncedDate": 1, "articleNo": 1, "barcode": 1,
        },
        sort=[("_id", -1)],
    )


def check_store(stores_col, assortment_col, store_code, barcode):
    store_no = query_store_no(stores_col, store_code)
    if store_no is None:
        return {
            "storeCode": store_code, "storeNo": None, "status": STATUS_STORE_NOT_FOUND,
            "articleNo": None,
            "solution": f"Can not find {store_code} pls check storeCode input",
        }

    doc = query_assortment(assortment_col, store_no, barcode)
    if doc is None:
        return {
            "storeCode": store_code, "storeNo": store_no, "status": STATUS_NOT_SYNCED,
            "articleNo": None,
            "solution": "pls export DB to re-check on Assortment table. If not found Run POS-Assortment on Jenkins",
        }

    return {
        "storeCode": store_code, "storeNo": store_no, "status": STATUS_SYNCED,
        "articleNo": doc.get("articleNo"),
        "solution": "-",
    }


def check_ticket(stores_col, assortment_col, ticket):
    return [
        check_store(stores_col, assortment_col, store_code, ticket["barcode"])
        for store_code in ticket["store_codes"]
    ]


def result_signature(results):
    return ",".join(f"{r['storeCode']}:{r['status']}" for r in results)


def already_reported(base_url, token, issue_key, signature):
    comments = jira_request(
        base_url, token, f"/rest/api/2/issue/{issue_key}/comment?orderBy=-created&maxResults=1"
    ).get("comments", [])
    if not comments:
        return False
    last_body = comments[0].get("body", "")
    if MARKER not in last_body:
        return False
    return f"signature: {signature}" in last_body


def format_comment(ticket, results):
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %z")
    lines = [
        f"*{MARKER}* ({now})",
        f"barcode: {ticket['barcode']}",
        "|| storeCode | storeNo | status | articleNo | solution ||",
    ]
    for r in results:
        lines.append(
            f"| {r['storeCode']} | {r['storeNo'] or '-'} | {r['status']} | "
            f"{r['articleNo'] or '-'} | {r['solution']} |"
        )
    if all(r["status"] == STATUS_SYNCED for r in results):
        lines.append("ทุกร้านค้า sync assortment แล้วครับ ปิด ticket อัตโนมัติ")
    else:
        lines.append("ยังมีร้านค้าที่ยังไม่ sync — ดู solution ของแต่ละแถวด้านบน")
    lines.append(f"signature: {result_signature(results)}")
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
    stores_col = mongo_client[DB_NAME][STORES_COLLECTION]
    assortment_col = mongo_client[DB_NAME][ASSORTMENT_COLLECTION]

    issues = search_open_assortment_tickets(base_url, token)
    if not issues:
        print("No open POS Assortment tickets found.")
        return

    for issue in issues:
        ticket = parse_ticket(issue)
        if not ticket:
            print(f"[{issue['key']}] could not parse barcode/store codes from description, skipping")
            continue

        results = check_ticket(stores_col, assortment_col, ticket)
        needs_work = any(r["status"] != STATUS_SYNCED for r in results)
        current_status = issue["fields"]["status"]["name"]
        current_assignee = issue["fields"].get("assignee")

        print(f"[{ticket['key']}] {ticket['summary']} (status={current_status})")
        synced = sum(1 for r in results if r["status"] == STATUS_SYNCED)
        print(f"  barcode={ticket['barcode']} stores={len(results)} synced={synced} needsWork={needs_work}")
        for r in results:
            print(f"    storeCode={r['storeCode']} storeNo={r['storeNo']} status={r['status']}")

        comment = format_comment(ticket, results)
        signature = result_signature(results)

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

        if already_reported(base_url, token, ticket["key"], signature):
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
