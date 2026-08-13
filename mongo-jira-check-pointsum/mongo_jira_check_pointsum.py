#!/usr/bin/env python3
"""Daily check: open Jira "Point Sum" reconcile tickets (project SUP, label
PS_Front) — re-run the member's point-sum-vs-transaction aggregate against
MongoDB, and if the numbers now match, close the ticket automatically.
(Companion to rsp_sync_check.py — same idea, different data source: Mongo
instead of GCP Cloud Logging.)

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
  3. A MongoDB connection string with read access to the `membership` DB,
     supplied via ONE of (checked in order):
       a. env var MONGO_URI
       b. ~/.mongo_jira_check.json's "mongo_uri" field
     Use the read-only PROD connection string documented in Confluence
     ("Tooling Onboarding Checklist", space TOOK) — the `support_read_only`
     account. Never commit this string anywhere; it belongs only in your
     local config file / env var.

Per ticket (doc_no in the description = memberId):
  - run the Point Sum aggregate (latest 10 non-EXPIRED docs' point total vs
    ACTIVE totalPoint) against MongoDB `membership.points`
  - if they're equal (isEqual=true): assign to whoever's token ran the
    check (only if unassigned — never overwrites an existing assignee),
    post a summary comment, and transition -> Close with
    resolution="Won't Do" and Fix Version/s="Won't Fix Release" (matches
    how the team already closes these tickets by hand, e.g. SUP-13422)
  - if they still differ: leave the ticket untouched (per the team's
    Confluence runbook, a same-day diff usually clears once BigQuery
    catches up overnight — no action needed until it's been open longer)
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
    "front-automation-hub/main/mongo-jira-check-pointsum/VERSION"
)

JQL = (
    'project = SUP AND labels = PS_Front AND summary ~ "\\"Point Sum\\"" '
    "AND statusCategory != Done ORDER BY created ASC"
)
MARKER = "Auto Point Sum Check"
LOCAL_CONFIG_PATH = Path.home() / ".mongo_jira_check.json"
RSP_CONFIG_PATH = Path.home() / ".rsp_sync_check.json"

DB_NAME = "membership"
COLLECTION_NAME = "points"

RESOLUTION_WONT_DO = "Won't Do"
FIX_VERSION_WONT_FIX = "Won't Fix Release"
TRANSITION_CLOSE = "Close"

DRY_RUN = "--dry-run" in sys.argv
SKIP_UPDATE_CHECK = "--skip-update-check" in sys.argv

DOC_NO_RE = re.compile(r"\|\|\s*doc_no\s*\|\s*([^\|]+?)\s*\|")
DOC_TYPE_RE = re.compile(r"\|\|\s*doc_type\s*\|\s*([^\|]+?)\s*\|")
DOC_SUBTYPE_RE = re.compile(r"\|\|\s*doc_subtype\s*\|\s*([^\|]+?)\s*\|")


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


def search_open_point_sum_tickets(base_url, token):
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
    doc_type_m = DOC_TYPE_RE.search(desc)
    doc_subtype_m = DOC_SUBTYPE_RE.search(desc)
    return {
        "key": issue["key"],
        "summary": issue["fields"]["summary"],
        "member_id": doc_no_m.group(1),
        "doc_type": doc_type_m.group(1) if doc_type_m else None,
        "doc_subtype": doc_subtype_m.group(1) if doc_subtype_m else None,
    }


def query_member_point(collection, member_id):
    pipeline = [
        {"$match": {"memberId": member_id}},
        {
            "$facet": {
                "latestDocs": [
                    {"$match": {"status": {"$ne": "EXPIRED"}}},
                    {"$sort": {"_id": -1}},
                    {"$limit": 10},
                ],
                "totalPoints": [
                    {"$match": {"status": "ACTIVE"}},
                    {"$group": {"_id": "$memberId", "totalPoint": {"$sum": "$point"}}},
                ],
            }
        },
        {"$unwind": "$totalPoints"},
        {
            "$project": {
                "memberId": "$totalPoints._id",
                "point": {"$sum": "$latestDocs.point"},
                "totalPoint": "$totalPoints.totalPoint",
                "reserve": {"$arrayElemAt": ["$latestDocs.reserve", 0]},
                "status": {"$arrayElemAt": ["$latestDocs.status", 0]},
                "isEqual": {
                    "$eq": [{"$sum": "$latestDocs.point"}, "$totalPoints.totalPoint"]
                },
            }
        },
    ]
    result = list(collection.aggregate(pipeline, maxTimeMS=20000))
    return result[0] if result else None


def format_close_comment(ticket, agg, now):
    lines = [
        f"*{MARKER}* ({now})",
        f"memberId: {ticket['member_id']}",
        f"point (sum of latest 10 non-EXPIRED): {agg['point']}",
        f"totalPoint (sum of ACTIVE): {agg['totalPoint']}",
        "ข้อมูล synced แล้วครับ ปิด ticket อัตโนมัติ",
    ]
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

    issues = search_open_point_sum_tickets(base_url, token)
    if not issues:
        print("No open Point Sum tickets found.")
        return

    for issue in issues:
        ticket = parse_ticket(issue)
        if not ticket:
            print(f"[{issue['key']}] could not parse doc_no from description, skipping")
            continue

        agg = query_member_point(collection, ticket["member_id"])
        if agg is None:
            print(f"[{ticket['key']}] memberId={ticket['member_id']}: no ACTIVE point record found in Mongo, skipping")
            continue

        current_assignee = issue["fields"].get("assignee")
        print(f"[{ticket['key']}] {ticket['summary']}")
        print(f"  memberId={ticket['member_id']} point={agg['point']} totalPoint={agg['totalPoint']} isEqual={agg['isEqual']}")

        if not agg["isEqual"]:
            print("  still diff — leaving open (per runbook, usually clears once BigQuery catches up)")
            continue

        comment = format_close_comment(ticket, agg, datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %z"))

        if DRY_RUN:
            if current_assignee:
                print(f"  assignee already set ({current_assignee.get('displayName')}), would leave unchanged")
            else:
                print(f"  --- would assign to {display_name} ({username}) ---")
            print("  --- would post comment ---")
            print("  " + comment.replace("\n", "\n  "))
            print(
                f"  --- would transition -> {TRANSITION_CLOSE} "
                f"(resolution='{RESOLUTION_WONT_DO}', fixVersion='{FIX_VERSION_WONT_FIX}') ---"
            )
            continue

        if current_assignee:
            print(f"  assignee already set ({current_assignee.get('displayName')}), leaving unchanged")
        else:
            set_assignee(base_url, token, ticket["key"], username)
            print(f"  assigned to {display_name} ({username})")

        post_comment(base_url, token, ticket["key"], comment)
        print("  comment posted")

        tid = get_transition_id(base_url, token, ticket["key"], TRANSITION_CLOSE)
        if tid:
            transition_issue(
                base_url, token, ticket["key"], tid,
                fields={
                    "resolution": {"name": RESOLUTION_WONT_DO},
                    "fixVersions": [{"name": FIX_VERSION_WONT_FIX}],
                },
            )
            print(
                f"  status: -> {TRANSITION_CLOSE}, "
                f"resolution='{RESOLUTION_WONT_DO}', fixVersion='{FIX_VERSION_WONT_FIX}'"
            )
        else:
            print(f"  WARNING: '{TRANSITION_CLOSE}' transition not available, status unchanged")


if __name__ == "__main__":
    main()
