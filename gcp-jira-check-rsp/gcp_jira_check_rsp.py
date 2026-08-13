#!/usr/bin/env python3
"""Daily check: which stores on open Jira "RSP Sync" tickets haven't synced yet,
per GCP PosApp logs, and comment the status back on the ticket.

Runs on ANY teammate's machine — no dependency on Claude Code. Each person
needs their own:
  1. gcloud CLI installed + `gcloud auth login` + read access to Cloud
     Logging on the tdshop-prod project (ask an admin if you get a
     permission error).
  2. A Jira Personal Access Token, supplied via ONE of (checked in order):
       a. env vars JIRA_URL / JIRA_PERSONAL_TOKEN
       b. a config file at ~/.rsp_sync_check.json:
            {"jira_url": "https://jira.tdshop.io/", "jira_token": "..."}
       c. (fallback, for Claude Code users) ~/.claude.json's
          mcpServers.mcp-atlassian.env block

On each live run:
  - the ticket is assigned to whoever's token ran the check, but ONLY if
    it doesn't already have an assignee (never overwrites an existing one)
  - if status is "Open" it always transitions to "In Progress" first
    (whether or not stores are still missing — a ticket found already
    fully synced while still Open passes straight through to Close below
    in the same run, it doesn't get stuck waiting on a separate pass)
  - once in "In Progress": if stores are still missing, adds the
    "Impediment" flag; if all stores are now synced, removes the flag
    (never left on when closing) and transitions to "Close" with
    resolution="Won't Do" and Fix Version/s="Won't Fix Release" (matches
    how the team already closes these tickets by hand)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_VERSION = "1.2.4"
VERSION_URL = (
    "https://raw.githubusercontent.com/natingkaninantanaxyt-web/"
    "front-automation-hub/main/gcp-jira-check-rsp/VERSION"
)

GCP_PROJECT = "tdshop-prod"
LOG_NAME = "projects/tdshop-prod/logs/PosApp"
LOG_EVENT = "FetchRetailPrice"
JQL = (
    'project = SUP AND labels = PS_Front AND summary ~ "\\"RSP Sync\\"" '
    "AND statusCategory != Done ORDER BY created ASC"
)
MARKER = "Auto RSP Sync Check"
LOCAL_CONFIG_PATH = Path.home() / ".rsp_sync_check.json"

FLAG_VALUE = "Impediment"
RESOLUTION_WONT_DO = "Won't Do"
FIX_VERSION_WONT_FIX = "Won't Fix Release"
TRANSITION_IN_PROGRESS = "In Progress"
TRANSITION_CLOSE = "Close"

DRY_RUN = "--dry-run" in sys.argv
SKIP_UPDATE_CHECK = "--skip-update-check" in sys.argv


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
            print("  Download the newest copy from the RSP Sync Check page before continuing —")
            print("  results/behavior may be wrong or incomplete on this old version.")
            print("=" * 70)
            answer = input("Continue anyway with the outdated version? [y/N]: ").strip().lower()
            if answer != "y":
                sys.exit(1)
            print()
    except Exception:
        return  # best-effort only, never crash the run over the update check


def load_jira_creds():
    url = os.environ.get("JIRA_URL")
    token = os.environ.get("JIRA_PERSONAL_TOKEN")
    if url and token:
        return url.rstrip("/"), token, "environment variables"

    if LOCAL_CONFIG_PATH.exists():
        try:
            cfg = json.loads(LOCAL_CONFIG_PATH.read_text())
            url, token = cfg.get("jira_url"), cfg.get("jira_token")
            if url and token:
                return url.rstrip("/"), token, str(LOCAL_CONFIG_PATH)
        except (json.JSONDecodeError, OSError):
            pass

    claude_cfg_path = Path.home() / ".claude.json"
    if claude_cfg_path.exists():
        try:
            cfg = json.loads(claude_cfg_path.read_text())
            env = cfg.get("mcpServers", {}).get("mcp-atlassian", {}).get("env", {})
            url, token = env.get("JIRA_URL"), env.get("JIRA_PERSONAL_TOKEN")
            if url and token:
                return url.rstrip("/"), token, "~/.claude.json (mcp-atlassian)"
        except (json.JSONDecodeError, OSError):
            pass

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


def check_gcloud_ready():
    if not shutil.which("gcloud"):
        fail(
            "`gcloud` CLI not found on this machine.\n"
            "  Install it from: https://cloud.google.com/sdk/docs/install"
        )
    proc = subprocess.run(["gcloud", "config", "get-value", "account"], capture_output=True, text=True)
    account = proc.stdout.strip()
    if not account or account == "(unset)":
        fail("No active gcloud account.\n  Run: gcloud auth login")
    return account


def jira_request(base_url, token, path, method="GET", body=None):
    # Uses curl (system trust store) instead of urllib, whose bundled
    # certifi CA list may not include this org's internal CA. The token is
    # passed via a curl -K config file (mode 600), not argv, so it never
    # shows up in `ps`.
    #
    # -w appends "\n<http_code>" after the body so we can detect HTTP-level
    # errors (400/403/404/...) ourselves — curl's own exit code stays 0 for
    # those (it only reflects transport failures), so without this a failed
    # request would silently look like success.
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
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            fail(
                f"Timed out waiting for Jira ({method} {path}) after 30s.\n"
                "  Check your network/VPN connection to jira.tdshop.io and try again."
            )
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


def search_open_rsp_tickets(base_url, token):
    body = {
        "jql": JQL,
        "fields": ["summary", "description", "status", "assignee"],
        "maxResults": 50,
    }
    result = jira_request(base_url, token, "/rest/api/2/search", "POST", body)
    return result.get("issues", [])


def parse_ticket(issue):
    desc = issue["fields"]["description"] or ""
    barcode_m = re.search(r"Oldest barcode\s*\|\s*(\d+)", desc)
    stores_m = re.search(r"Store Codes\s*\|\s*([A-Z0-9,]+)", desc)
    date_m = re.search(r"Effective Date\s*\|\s*([\d-]+)", desc)
    if not (barcode_m and stores_m and date_m):
        return None
    stores = sorted(set(stores_m.group(1).split(",")))
    return {
        "key": issue["key"],
        "summary": issue["fields"]["summary"],
        "barcode": barcode_m.group(1),
        "effective_date": date_m.group(1),
        "stores": stores,
    }


def query_synced_stores(ticket):
    store_clause = " OR ".join(f'"{s}"' for s in ticket["stores"])
    start = (
        datetime.strptime(ticket["effective_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        - timedelta(days=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_filter = (
        f'resource.type="global"\n'
        f'logName="{LOG_NAME}"\n'
        f'labels.storeCode=({store_clause})\n'
        f'"{ticket["barcode"]}"\n'
        f'labels.event="{LOG_EVENT}"\n'
        f'timestamp>="{start}"\n'
    )
    proc = subprocess.run(
        [
            "gcloud", "logging", "read", log_filter,
            f"--project={GCP_PROJECT}", "--limit=2000", "--format=json",
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if "PERMISSION_DENIED" in stderr or "403" in stderr:
            fail(
                f"Permission denied reading logs on project '{GCP_PROJECT}'.\n"
                "  Ask a GCP admin to grant you the 'Logs Viewer' (roles/logging.viewer) role.\n"
                f"  Raw error: {stderr}"
            )
        fail(f"`gcloud logging read` failed:\n  {stderr}")
    entries = json.loads(proc.stdout) if proc.stdout.strip() else []
    found = set()
    for e in entries:
        sc = (e.get("labels") or {}).get("storeCode")
        if sc:
            found.add(sc)
    return found


def already_reported(base_url, token, issue_key, missing):
    comments = jira_request(
        base_url, token, f"/rest/api/2/issue/{issue_key}/comment?orderBy=-created&maxResults=1"
    ).get("comments", [])
    if not comments:
        return False
    last_body = comments[0].get("body", "")
    if MARKER not in last_body:
        return False
    expected_tail = ",".join(missing) if missing else "none"
    return expected_tail in last_body


def post_comment(base_url, token, issue_key, body):
    jira_request(base_url, token, f"/rest/api/2/issue/{issue_key}/comment", "POST", {"body": body})


def format_comment(ticket, missing, found_count):
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %z")
    if missing:
        lines = [
            f"*{MARKER}* ({now})",
            f"Effective Date: {ticket['effective_date']} | Barcode: {ticket['barcode']}",
            f"Synced: {found_count}/{len(ticket['stores'])}",
            f"Not yet synced ({len(missing)}): {','.join(missing)}",
        ]
    else:
        lines = [
            f"*{MARKER}* ({now})",
            f"Effective Date: {ticket['effective_date']} | Barcode: {ticket['barcode']}",
            f"Synced: {found_count}/{len(ticket['stores'])} - all stores synced.",
        ]
    return "\n".join(lines)


def main():
    print(f"{Path(__file__).name} v{SCRIPT_VERSION}\n")
    check_for_updates()

    account = check_gcloud_ready()
    base_url, token, creds_source = load_jira_creds()
    username, display_name = get_current_user(base_url, token)
    print(f"gcloud account: {account}")
    print(f"Jira credentials from: {creds_source}")
    print(f"Running as: {display_name} ({username})\n")

    issues = search_open_rsp_tickets(base_url, token)
    if not issues:
        print("No open RSP Sync tickets found.")
        return

    for issue in issues:
        ticket = parse_ticket(issue)
        if not ticket:
            print(f"[{issue['key']}] could not parse description, skipping")
            continue

        found = query_synced_stores(ticket)
        missing = sorted(set(ticket["stores"]) - found)
        found_count = len(ticket["stores"]) - len(missing)

        current_status = issue["fields"]["status"]["name"]
        current_assignee = issue["fields"].get("assignee")

        print(f"[{ticket['key']}] {ticket['summary']} (status={current_status})")
        print(f"  stores={len(ticket['stores'])} synced={found_count} missing={len(missing)} {missing}")

        comment = format_comment(ticket, missing, found_count)

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
                if missing:
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

        if already_reported(base_url, token, ticket["key"], missing):
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
        # ticket that was already In Progress but never got flagged — e.g. because an
        # earlier run hit the flag-endpoint bug fixed in v1.2.1 — gets caught up here.
        if current_status == TRANSITION_IN_PROGRESS:
            if missing:
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
