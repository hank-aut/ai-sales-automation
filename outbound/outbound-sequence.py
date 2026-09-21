#!/usr/bin/env python3
"""
outbound-sequence.py — Manage email sequences (follow-ups on Day 4 and Day 10).

When Hank APPROVEs a prospect:
  - Email 1 gets sent (or stays as Outlook draft for manual send)
  - Email 2 is scheduled for Day 4
  - Email 3 is scheduled for Day 10

This script checks for due follow-ups and creates Outlook drafts for them.

Usage:
  python3 outbound-sequence.py --check          # Check for due follow-ups, create drafts
  python3 outbound-sequence.py --approve "Co"   # Mark a prospect as approved, schedule follow-ups
  python3 outbound-sequence.py --skip "Co"      # Mark as skipped
  python3 outbound-sequence.py --stop "Co"      # Stop sequence (reply received)
  python3 outbound-sequence.py --status         # Show all active sequences
"""

import json
import sys
sys.path.insert(0, str(__import__('pathlib').Path.home() / '.local/bin'))
from email_safety import validate_email_before_send
from reply_classify import detect_departure
import subprocess
import sys
import html
import fcntl
from datetime import datetime, timezone, timedelta
from pathlib import Path

# State dir resolution: prefer openclaw workspace (works under launchd / SSH where iCloud TCC blocks reads).
def _resolve_vault():
    for d in [Path.home() / ".openclaw/workspace", Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/hank-vault"]:
        try:
            test = d / ".rw_test"
            d.mkdir(parents=True, exist_ok=True)
            test.write_text("ok"); test.unlink()
            return d
        except (PermissionError, OSError):
            continue
    return Path("/tmp")
VAULT = _resolve_vault()
STATE_DIR = VAULT / "outbound-v2"
SEQUENCE_DIR = STATE_DIR / "sequences"
PROSPECTS_FILE = STATE_DIR / "pipeline-results.json"
EXCLUSION_FILE = STATE_DIR / "exclusion-list.json"

# Graph API auth lives on the OpenClaw remote (teams@remote-host.example.com). The engine is designed to
# run ON that remote — so it calls outlook-graph-auth.sh locally, no SSH hop. If invoked on a
# different host (e.g. the user's Mac), Graph calls will fail gracefully.
LOCAL_PATH = "export PATH=$HOME/.local/bin:$HOME/.nvm/versions/node/v22.22.2/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
HANK_SF_USER_ID = "005UM000004VyQbYAK"

# Lock file lives outside iCloud — it's a cross-process mutex that doesn't need cross-machine
# visibility, and iCloud sync + flock are unhappy together.
LOCK_FILE = Path.home() / ".openclaw/workspace/outbound-v2/.sequence-check.lock"
# Minimum seconds between processing the same sequence (prevents double-sends from concurrent runs)
MIN_CHECK_INTERVAL_SECONDS = 3600

SIGNATURE_HTML = '''<span style="font-family:Aptos,sans-serif;font-size:11pt;">
<a href="mailto:you@example.com" style="color:#0078D4;font-weight:bold;text-decoration:none;">Hank Autrey</a><br>
<b>Sr. Business Development, datacenterHawk</b><br>
555-555-5555 | datacenterhawk.com<br>
14785 Preston Rd., Suite 470 | Dallas, TX 75254
</span>'''

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv


def log(msg):
    if VERBOSE:
        print(f"  [seq] {msg}", file=sys.stderr)


LOCAL_SEQUENCE_DIR = Path.home() / ".openclaw/workspace/outbound-v2/sequences"


def _all_sequence_dirs():
    """Both possible locations — iCloud (primary, cross-machine) and openclaw workspace (fallback)."""
    return [SEQUENCE_DIR, LOCAL_SEQUENCE_DIR]


def load_sequences():
    """Load all sequence files from both the iCloud vault and the openclaw workspace."""
    sequences = {}
    for d in _all_sequence_dirs():
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            continue
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                data["_sequence_path"] = str(f)  # remember which dir each came from
                sequences[f.stem] = data
            except Exception:
                pass
    return sequences


def save_sequence(slug, data):
    """Write to the same dir the sequence was loaded from (preserves location), else iCloud."""
    target = data.get("_sequence_path")
    if target:
        try:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            # strip internal field before serializing
            to_write = {k: v for k, v in data.items() if k != "_sequence_path"}
            Path(target).write_text(json.dumps(to_write, indent=2, default=str))
            return
        except Exception:
            pass
    # Fallback: write to iCloud primary, then openclaw
    for d in _all_sequence_dirs():
        try:
            d.mkdir(parents=True, exist_ok=True)
            to_write = {k: v for k, v in data.items() if k != "_sequence_path"}
            (d / f"{slug}.json").write_text(json.dumps(to_write, indent=2, default=str))
            return
        except Exception:
            continue


def body_to_html(body_text):
    escaped = html.escape(body_text)
    html_body = escaped.replace("\n", "<br>")
    return f"""<html><body>
<div style="font-family:Aptos,sans-serif;font-size:11pt;">
{html_body}
</div>
<br>
{SIGNATURE_HTML}
</body></html>"""


def graph_api_call(method, endpoint, payload=None):
    """Make an authenticated Graph API call. Runs locally on the OpenClaw remote where
    outlook-graph-auth.sh + tokens live. Returns parsed JSON.
    """
    base_cmd = (
        f"{LOCAL_PATH} && "
        f"TOKEN=$(outlook-graph-auth.sh token) && "
        f"curl -s -X {method} 'https://graph.microsoft.com/v1.0{endpoint}' "
        f"-H \"Authorization: Bearer $TOKEN\""
    )
    if payload:
        payload_json = json.dumps(payload) if isinstance(payload, dict) else payload
        cmd = base_cmd + " -H 'Content-Type: application/json' -d @-"
        result = subprocess.run(
            ["bash", "-c", cmd],
            input=payload_json.encode("utf-8") if isinstance(payload_json, str) else payload_json,
            capture_output=True, timeout=30,
        )
    else:
        result = subprocess.run(["bash", "-c", base_cmd], capture_output=True, timeout=30)

    stdout = result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout
    return json.loads(stdout) if stdout.strip() else {}


def find_sent_message_id(to_email, subject, sent_after=None):
    """Find the original sent message in Outlook Sent Items by recipient + subject.

    The old OData filter (`subject eq '...' and toRecipients/any(...)`) is unreliable —
    it silently returns empty when the subject contains '+' (e.g. "datacenterHawk + X")
    and errors on the lambda combo — which is why follow-ups were landing un-threaded.
    Instead we bound by sentDateTime (a simple, valid filter) and match subject +
    recipient in Python. With a floor we scan ASCENDING (Email 1 is the oldest message
    at/after the floor, so it's first); without one we scan DESCENDING (newest first,
    for the just-sent case)."""
    import urllib.parse
    floor = None
    if sent_after:
        try:
            s = str(sent_after).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s if "T" in s else s + "T00:00:00+00:00")
            floor = (dt - timedelta(minutes=5)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            floor = None

    endpoint = "/me/mailFolders/SentItems/messages?"
    if floor:
        endpoint += "$filter=" + urllib.parse.quote(f"sentDateTime ge {floor}") + "&"
    order = "sentDateTime asc" if floor else "sentDateTime desc"
    endpoint += ("$top=50&$orderby=" + urllib.parse.quote(order)
                 + "&$select=id,subject,sentDateTime,conversationId,toRecipients")

    want = (subject or "").strip().lower()
    tgt = (to_email or "").strip().lower()
    try:
        data = graph_api_call("GET", endpoint)
        for msg in data.get("value", []):
            if msg.get("subject", "").strip().lower() != want:
                continue
            recips = [(r.get("emailAddress") or {}).get("address", "").lower()
                      for r in (msg.get("toRecipients") or [])]
            if tgt in recips:
                return {
                    "id": msg["id"],
                    "conversationId": msg.get("conversationId", ""),
                    "sentDateTime": msg.get("sentDateTime", ""),
                }
    except Exception as e:
        log(f"find_sent_message_id error: {e}")
    return None


def create_outlook_draft(to_email, subject, body_text):
    """Create a NEW Outlook draft (for Email 1 only)."""
    # SAFETY CHECK
    from email_safety import validate_email_before_send
    ok, err = validate_email_before_send(subject, body_text, to_email)
    if not ok:
        print(f"   🚫 {err}")
        return {"success": False, "error": err}

    html_body = body_to_html(body_text)
    payload = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": [{"emailAddress": {"address": to_email}}],
        "isDraft": True,
    }

    try:
        response = graph_api_call("POST", "/me/messages", payload)
        draft_id = response.get("id", "")
        if draft_id:
            return {"success": True, "draft_id": draft_id}
        return {"success": False, "error": response.get("error", {}).get("message", "unknown")}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _reply_fragment(body_text):
    """New reply text + signature as an HTML fragment, to sit ABOVE the quoted history."""
    safe = html.escape(body_text).replace("\n", "<br>")
    return (
        '<div style="font-family:Aptos,sans-serif;font-size:11pt;">'
        f'{safe}</div><br>{SIGNATURE_HTML}<br>'
    )


def _merge_above_quote(fragment, quoted_html):
    """Insert the reply fragment just inside <body> of Graph's quoted reply, so the
    result is new text on top, quoted original below. Falls back to prepend."""
    if not quoted_html:
        return f"<html><body>{fragment}</body></html>"
    low = quoted_html.lower()
    i = low.find("<body")
    if i != -1:
        j = quoted_html.find(">", i)
        if j != -1:
            return quoted_html[:j + 1] + fragment + quoted_html[j + 1:]
    return fragment + quoted_html


def create_outlook_reply_draft(original_message_id, body_text, to_email, subject):
    """Create a reply draft on an existing message (for follow-ups).
    Uses createReply for proper In-Reply-To/References headers + threading, and
    prepends our text above the quoted original (instead of wiping it).
    Validates against the real recipient + subject (not a placeholder)."""
    from email_safety import validate_email_before_send
    ok, err = validate_email_before_send(subject, body_text, to_email)
    if not ok:
        print(f"   🚫 {err}")
        return {"success": False, "error": err}

    try:
        # Step 1: createReply — makes a draft in the same thread (with quoted original)
        reply_resp = graph_api_call(
            "POST",
            f"/me/messages/{original_message_id}/createReply",
            {"comment": ""},
        )
        reply_draft_id = reply_resp.get("id", "")
        if not reply_draft_id:
            err = reply_resp.get("error", {}).get("message", "createReply failed")
            return {"success": False, "error": err}

        # Step 2: fetch Graph's quoted body and prepend our follow-up text above it
        existing = graph_api_call("GET", f"/me/messages/{reply_draft_id}?$select=body")
        quoted_html = (existing.get("body") or {}).get("content", "")
        combined = _merge_above_quote(_reply_fragment(body_text), quoted_html)
        # createReply on a message WE sent addresses the reply back to the sender (us),
        # so override toRecipients to the prospect — otherwise the follow-up draft is
        # addressed to Hank instead of the lead.
        patch_resp = graph_api_call(
            "PATCH",
            f"/me/messages/{reply_draft_id}",
            {"body": {"contentType": "HTML", "content": combined},
             "toRecipients": [{"emailAddress": {"address": to_email}}]},
        )
        if patch_resp.get("id"):
            return {"success": True, "draft_id": reply_draft_id}
        return {"success": False, "error": patch_resp.get("error", {}).get("message", "PATCH failed")}
    except Exception as e:
        return {"success": False, "error": str(e)}


def send_outlook_reply(original_message_id, body_text, to_email, subject):
    """Build the threaded reply draft (correct recipient + quoted history) and SEND it.
    Used when outbound follow-ups auto-send (Hank's setting as of 2026-06-09), mirroring
    inbound. Returns {success, message_id} or {success: False, error}."""
    res = create_outlook_reply_draft(original_message_id, body_text, to_email, subject)
    if not res.get("success"):
        return res
    did = res["draft_id"]
    base_cmd = (
        f"{LOCAL_PATH} && TOKEN=$(outlook-graph-auth.sh token) && "
        f"curl -s -w '\\n%{{http_code}}' -X POST "
        f"'https://graph.microsoft.com/v1.0/me/messages/{did}/send' "
        f"-H \"Authorization: Bearer $TOKEN\" -H 'Content-Length: 0'"
    )
    r = subprocess.run(["bash", "-c", base_cmd], capture_output=True, text=True, timeout=30)
    code = r.stdout.strip().split("\n")[-1] if r.stdout.strip() else ""
    if code in ("200", "202", "204"):
        return {"success": True, "message_id": did}
    return {"success": False, "error": f"send HTTP {code}: {r.stdout[:160]}"}


def notify_telegram(message):
    """Post a notification to Slack #rudy-outbound. Telegram was retired 2026-05-14;
    function name kept for call-site compatibility. Posts as the Rudy bot via slack_send.sh."""
    try:
        slack_send = Path.home() / ".local/bin/slack_send.sh"
        subprocess.run(
            [str(slack_send), "C0000000000", message],
            capture_output=True, timeout=15,
        )
    except Exception as e:
        log(f"Slack notify error: {e}")


def sf_query_records(soql):
    """Run a SOQL query and return records list."""
    import re as _re
    result = subprocess.run(
        ["sf", "data", "query", "--query", soql,
         "--target-org", "you@example.com", "--json"],
        capture_output=True, text=True, timeout=60,
    )
    raw = _re.sub(r'\x1b\[[0-9;]*m', '', result.stdout)
    try:
        idx = raw.index('{')
        data = json.loads(raw[idx:])
        return data.get("result", {}).get("records", [])
    except Exception:
        return []


def find_sf_account(account_name, email_domain):
    """Find SF account by domain first, then name. Also checks if contacts with
    this domain exist under a different account (parent company scenario)."""
    # 1. Search by domain in Website field
    if email_domain:
        records = sf_query_records(
            f"SELECT Id, Name, OwnerId FROM Account "
            f"WHERE Website LIKE '%{email_domain}%' LIMIT 1"
        )
        if records:
            return records[0]

    # 2. Search by exact name
    encoded_name = account_name.replace("'", "\\'")
    records = sf_query_records(
        f"SELECT Id, Name, OwnerId FROM Account "
        f"WHERE Name = '{encoded_name}' LIMIT 1"
    )
    if records:
        return records[0]

    # 3. Search by LIKE name (catches "Castlelake" → "Castlelake L.P.")
    records = sf_query_records(
        f"SELECT Id, Name, OwnerId FROM Account "
        f"WHERE Name LIKE '%{encoded_name}%' LIMIT 1"
    )
    if records:
        return records[0]

    # 4. Check if contacts with this email domain exist under a DIFFERENT account
    # (catches Fidium → Consolidated Communications scenario)
    if email_domain:
        contacts = sf_query_records(
            f"SELECT AccountId, Account.Name FROM Contact "
            f"WHERE Email LIKE '%@{email_domain}' AND AccountId != null LIMIT 1"
        )
        if contacts:
            acct_id = contacts[0].get("AccountId")
            acct_name = (contacts[0].get("Account") or {}).get("Name", "?")
            log(f"Found contact with domain @{email_domain} under account '{acct_name}'")
            parent = sf_query_records(
                f"SELECT Id, Name, OwnerId FROM Account WHERE Id = '{acct_id}' LIMIT 1"
            )
            if parent:
                return parent[0]

    return None


def check_roe_for_followup(account_name, to_email, since_date):
    """Check SF for non-Hank activity on this account since the initial email.
    Returns (clear, detail_string).
    - clear=True: only Hank's activity (or none) → safe to follow up
    - clear=False: someone else touched this account → pause sequence"""
    try:
        domain = to_email.split("@")[1] if "@" in to_email else ""
        acct = find_sf_account(account_name, domain)
        if not acct:
            log(f"ROE: Account '{account_name}' not found in SF — skipping check")
            return True, "account not found in SF"

        acct_id = acct["Id"]
        acct_owner = acct.get("OwnerId", "")

        # Check Tasks (includes emails and calls) since the initial send
        since_str = since_date[:10] if since_date else "2026-01-01"
        tasks = sf_query_records(
            f"SELECT Id, Subject, OwnerId, Owner.Name, CreatedDate, TaskSubtype "
            f"FROM Task WHERE AccountId = '{acct_id}' "
            f"AND CreatedDate >= {since_str}T00:00:00Z "
            f"ORDER BY CreatedDate DESC LIMIT 10"
        )

        # Check Events since the initial send
        events = sf_query_records(
            f"SELECT Id, Subject, OwnerId, Owner.Name, CreatedDate "
            f"FROM Event WHERE AccountId = '{acct_id}' "
            f"AND CreatedDate >= {since_str}T00:00:00Z "
            f"ORDER BY CreatedDate DESC LIMIT 10"
        )

        # Check for non-Hank activity
        violations = []
        for t in tasks:
            owner_id = t.get("OwnerId", "")
            if owner_id != HANK_SF_USER_ID:
                owner_name = t.get("Owner", {}).get("Name", "Unknown") if t.get("Owner") else "Unknown"
                violations.append(
                    f"Task: {t.get('Subject', '?')} by {owner_name} on {t.get('CreatedDate', '?')[:10]}"
                )

        for e in events:
            owner_id = e.get("OwnerId", "")
            if owner_id != HANK_SF_USER_ID:
                owner_name = e.get("Owner", {}).get("Name", "Unknown") if e.get("Owner") else "Unknown"
                violations.append(
                    f"Event: {e.get('Subject', '?')} by {owner_name} on {e.get('CreatedDate', '?')[:10]}"
                )

        if violations:
            detail = "\n".join(f"   ⚠️  {v}" for v in violations)
            return False, detail
        return True, "clear"

    except Exception as e:
        log(f"ROE check error: {e}")
        return True, f"ROE check failed ({e}) — proceeding cautiously"


def approve_prospect(company_key):
    """Mark a prospect as approved and schedule follow-ups."""
    sequences = load_sequences()

    # Find matching sequence
    slug = None
    seq_data = None
    for s, data in sequences.items():
        if company_key.lower() in data.get("company", "").lower():
            slug = s
            seq_data = data
            break

    if not seq_data:
        print(f"No sequence found for '{company_key}'")
        print(f"Available: {', '.join(d.get('company', s) for s, d in sequences.items())}")
        return

    now = datetime.now(timezone.utc)
    emails = seq_data.get("emails", [])
    contact = seq_data.get("contact", {})
    to_email = contact.get("email", "")

    # Safety check: verify no reply already exists in inbox
    if to_email:
        reply = check_inbox_for_replies(to_email)
        if reply.get("has_reply"):
            print(f"🛑 BLOCKED: Already have a reply from {to_email}!")
            print(f"   Subject: {reply.get('subject', '?')}")
            print(f"   Date: {reply.get('date', '?')}")
            print(f"   Sequence NOT approved. Handle the reply first.")
            seq_data["status"] = "reply_received"
            seq_data["reply_subject"] = reply.get("subject", "")
            save_sequence(slug, seq_data)
            return

    # Find the original sent message ID for threading follow-ups
    subject_e1 = emails[0].get("subject", "") if emails else ""
    original_msg = find_sent_message_id(to_email, subject_e1)
    if original_msg:
        seq_data["original_message_id"] = original_msg["id"]
        seq_data["conversation_id"] = original_msg.get("conversationId", "")
        log(f"Found original message: {original_msg['id'][:30]}...")
    else:
        log(f"Could not find original sent message for {to_email} / {subject_e1}")

    # Schedule follow-ups
    seq_data["status"] = "active"
    seq_data["approved_at"] = now.isoformat()
    seq_data["email_status"] = {}

    for i, email in enumerate(emails):
        send_day = email.get("send_day", 0)
        send_date = (now + timedelta(days=send_day)).isoformat()
        seq_data["email_status"][f"email_{i+1}"] = {
            "scheduled_for": send_date,
            "send_day": send_day,
            "status": "sent" if send_day == 0 else "scheduled",
            "draft_id": None,
        }

    save_sequence(slug, seq_data)

    print(f"✅ Approved: {seq_data['company']}")
    print(f"   Email 1 (Day 0): Ready to send → {to_email}")
    for i, email in enumerate(emails[1:], 2):
        send_day = email.get("send_day", 0)
        send_date = (now + timedelta(days=send_day)).strftime("%Y-%m-%d")
        print(f"   Email {i} (Day {send_day}): Scheduled for {send_date}")

    # Update pipeline results
    if PROSPECTS_FILE.exists():
        state = json.loads(PROSPECTS_FILE.read_text())
        for key, prospect in state.items():
            if company_key.lower() in prospect.get("company", "").lower():
                prospect["status"] = "APPROVED"
                prospect["approved_at"] = now.isoformat()
                break
        PROSPECTS_FILE.write_text(json.dumps(state, indent=2, default=str))


def skip_prospect(company_key):
    """Mark prospect as skipped, add 30-day cooldown."""
    sequences = load_sequences()

    for slug, data in sequences.items():
        if company_key.lower() in data.get("company", "").lower():
            data["status"] = "skipped"
            data["skipped_at"] = datetime.now(timezone.utc).isoformat()
            data["cooldown_until"] = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            save_sequence(slug, data)
            print(f"⏭️  Skipped: {data['company']} (30-day cooldown)")

            # Update pipeline
            if PROSPECTS_FILE.exists():
                state = json.loads(PROSPECTS_FILE.read_text())
                for key, prospect in state.items():
                    if company_key.lower() in prospect.get("company", "").lower():
                        prospect["status"] = "SKIPPED"
                        break
                PROSPECTS_FILE.write_text(json.dumps(state, indent=2, default=str))
            return

    print(f"No sequence found for '{company_key}'")


def stop_sequence(company_key):
    """Stop a sequence (reply received)."""
    sequences = load_sequences()

    for slug, data in sequences.items():
        if company_key.lower() in data.get("company", "").lower():
            data["status"] = "reply_received"
            data["stopped_at"] = datetime.now(timezone.utc).isoformat()
            save_sequence(slug, data)

            company = data.get("company", "?")
            print(f"🛑 Stopped sequence: {company} (reply received)")
            notify_telegram(f"Reply received from {company} - sequence stopped")
            return

    print(f"No sequence found for '{company_key}'")


def check_inbox_for_replies(email_address, since_iso=None):
    """Check Outlook for messages from this prospect (= a reply).

    The filter MUST be URL-encoded — raw spaces/datetime in the URL silently break the
    query → empty result → missed reply. And do NOT add $orderby (Graph rejects
    ordering on a field other than the $filter field), so we sort + date-compare in
    Python. With since_iso, only count messages at/after the sequence start."""
    import urllib.parse
    floor = None
    if since_iso:
        try:
            floor = datetime.fromisoformat(str(since_iso).replace("Z", "+00:00"))
            if floor.tzinfo is None:  # naive -> assume UTC (Graph times are UTC/Z)
                floor = floor.replace(tzinfo=timezone.utc)
        except Exception:
            floor = None
    try:
        flt = f"from/emailAddress/address eq '{email_address}'"
        endpoint = ("/me/messages?$filter=" + urllib.parse.quote(flt)
                    + "&$top=10&$select=subject,receivedDateTime,from,bodyPreview")
        data = graph_api_call("GET", endpoint)
        msgs = data.get("value", [])
        msgs.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
        for m in msgs:
            rcv = m.get("receivedDateTime", "")
            if floor:
                # Only count messages confirmed to arrive after the sequence started
                # (naive-vs-aware comparison previously threw + was swallowed -> ancient
                #  email false-stopped sequences). Skip anything not confirmed >= floor.
                try:
                    rcv_dt = datetime.fromisoformat(rcv.replace("Z", "+00:00"))
                    if rcv_dt.tzinfo is None:
                        rcv_dt = rcv_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if rcv_dt < floor:
                    continue
            return {
                "has_reply": True,
                "subject": m.get("subject", ""),
                "date": rcv[:19],
                "preview": m.get("bodyPreview", ""),
                "count": len(msgs),
            }
        return {"has_reply": False}
    except Exception as e:
        log(f"Inbox check error for {email_address}: {e}")
        return {"has_reply": False, "error": str(e)}


def check_due_followups(send_mode=True):
    """Check for follow-up emails that are due and create Outlook drafts.
    ALWAYS checks inbox for replies first — auto-kills sequence if reply found.
    Uses file locking to prevent concurrent runs (April 7 2026 incident)."""

    # GLOBAL DEDUP — check if ANY machine ran --check recently
    # This file lives in iCloud (STATE_DIR) so both local and remote see it
    global_check_file = STATE_DIR / ".last-sequence-check"
    now_ts = datetime.now(timezone.utc)
    if global_check_file.exists():
        try:
            last_run = datetime.fromisoformat(global_check_file.read_text().strip())
            elapsed = (now_ts - last_run).total_seconds()
            if elapsed < MIN_CHECK_INTERVAL_SECONDS:
                print(f"⚠️  Sequence check already ran {int(elapsed)}s ago (min interval: {MIN_CHECK_INTERVAL_SECONDS}s). Skipping.")
                return
        except (ValueError, TypeError):
            pass

    # Write timestamp BEFORE processing — prevents race between local and remote
    global_check_file.parent.mkdir(parents=True, exist_ok=True)
    global_check_file.write_text(now_ts.isoformat())

    # Acquire exclusive lock — if another process on THIS machine is already running, exit
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        print("⚠️  Another --check is already running. Exiting to prevent duplicate sends.")
        lock_fd.close()
        return

    try:
        _check_due_followups_locked()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _check_due_followups_locked():
    """Inner function — runs under exclusive file lock."""
    sequences = load_sequences()
    now = datetime.now(timezone.utc)
    due_count = 0

    for slug, data in sequences.items():
        if data.get("status") != "active":
            continue

        # DEDUP GUARD — skip if this sequence was checked within MIN_CHECK_INTERVAL_SECONDS
        last_checked = data.get("last_checked_at")
        if last_checked:
            try:
                last_dt = datetime.fromisoformat(last_checked)
                if (now - last_dt).total_seconds() < MIN_CHECK_INTERVAL_SECONDS:
                    log(f"Skipping {data.get('company', '?')} — checked {int((now - last_dt).total_seconds())}s ago")
                    continue
            except (ValueError, TypeError):
                pass

        # Mark as checked NOW before doing anything — prevents re-processing even if we crash
        data["last_checked_at"] = now.isoformat()
        save_sequence(slug, data)

        # REPLY CHECK — scan inbox before doing anything else
        contact = data.get("contact", {})
        to_email = contact.get("email", "")
        company = data.get("company", "?")

        if to_email:
            log(f"Checking inbox for replies from {to_email}...")
            seq_start = data.get("approved_at") or data.get("sent_date") or data.get("created", "")
            reply = check_inbox_for_replies(to_email, seq_start)
            if reply.get("has_reply"):
                # AUTO-KILL: reply received
                data["status"] = "reply_received"
                data["stopped_at"] = now.isoformat()
                data["reply_subject"] = reply.get("subject", "")
                data["reply_date"] = reply.get("date", "")

                print(f"🛑 AUTO-STOP: {company} — reply received!")
                print(f"   From: {to_email}")
                print(f"   Subject: {reply.get('subject', '?')}")
                print(f"   Date: {reply.get('date', '?')}")

                # Detect "no longer with the company" auto-replies and flag for re-routing.
                is_dep, repl = detect_departure(
                    (reply.get("preview", "") + " " + reply.get("subject", "")))
                if is_dep:
                    data["departure_detected"] = True
                    rl = ("\nPossible replacement(s): " + ", ".join(repl)) if repl else ""
                    notify_telegram(
                        f"🚪 LEFT COMPANY — {company}\n"
                        f"{to_email} appears to have left. Outbound sequence stopped.{rl}\n"
                        f"Reply: \"{reply.get('preview', '')[:240]}\"\n"
                        f"→ Consider pivoting to a replacement contact."
                    )
                else:
                    notify_telegram(
                        f"REPLY RECEIVED from {company}\n"
                        f"From: {to_email}\n"
                        f"Subject: {reply.get('subject', '?')}\n"
                        f"Sequence auto-stopped."
                    )
                save_sequence(slug, data)
                continue  # Skip to next sequence

        emails = data.get("emails", [])
        email_status = data.get("email_status", {})
        original_message_id = data.get("original_message_id")
        sent_date = data.get("approved_at") or data.get("sent_date") or data.get("created", "")

        for i, email in enumerate(emails):
            key = f"email_{i+1}"
            status = email_status.get(key, {})

            if status.get("status") != "scheduled":
                continue

            scheduled = status.get("scheduled_for", "")
            if not scheduled:
                continue

            scheduled_dt = datetime.fromisoformat(scheduled)
            if now >= scheduled_dt:
                # This follow-up is due
                print(f"\n📧 Due: {company} - Email {i+1} (Day {email.get('send_day', '?')})")
                print(f"   To: {to_email}")

                # ROE CHECK — make sure nobody else touched this account
                roe_clear, roe_detail = check_roe_for_followup(company, to_email, sent_date)
                if not roe_clear:
                    print(f"   ⚠️  ROE VIOLATION — someone else has activity on this account:")
                    print(roe_detail)
                    print(f"   ⏸️  Sequence PAUSED for {company}")
                    data["status"] = "paused_roe"
                    data["paused_at"] = now.isoformat()
                    data["roe_violation"] = roe_detail
                    save_sequence(slug, data)
                    notify_telegram(
                        f"⚠️ ROE VIOLATION — sequence paused for {company}\n"
                        f"Someone else has activity on this account since your initial email:\n"
                        f"{roe_detail}\n"
                        f"Review and decide: --approve or --stop"
                    )
                    break  # Stop processing this sequence

                # If no original_message_id, try to find it now
                if not original_message_id and i > 0:
                    subject_e1 = emails[0].get("subject", "")
                    original_msg = find_sent_message_id(to_email, subject_e1, sent_date if sent_date else None)
                    if original_msg:
                        original_message_id = original_msg["id"]
                        data["original_message_id"] = original_message_id
                        data["conversation_id"] = original_msg.get("conversationId", "")
                        log(f"Found original message: {original_message_id[:30]}...")

                # If no original_message_id, try to find it (done just above). Follow-ups
                # MUST thread — never auto-send an unthreaded cold email.
                if i > 0 and not original_message_id:
                    print(f"   ⏸️  No original message found to thread onto — deferring Email {i+1}")
                    status["status"] = "deferred_no_thread"
                    email_status[key] = status
                    continue

                if send_mode and i > 0:
                    # AUTO-SEND threaded follow-up (no manual approval)
                    print(f"   📨 Sending threaded follow-up...")
                    result = send_outlook_reply(
                        original_message_id, email.get("body", ""), to_email, email.get("subject", ""))
                    if result.get("success"):
                        status["status"] = "sent"
                        status["message_id"] = result["message_id"]
                        status["sent_at"] = now.isoformat()
                        print(f"   ✅ Follow-up SENT (threaded)")
                        notify_telegram(
                            f"📤 Outbound follow-up SENT: {company} Email {i+1}\n"
                            f"To: {to_email}\nThreaded reply."
                        )
                        due_count += 1
                    else:
                        print(f"   ❌ Send failed: {result.get('error', '?')}")
                        status["status"] = "send_failed"
                        status["error"] = result.get("error", "")
                        notify_telegram(
                            f"⚠️ Outbound follow-up FAILED: {company} Email {i+1} — {result.get('error', '')}")
                else:
                    # DRAFT mode (opt-in via --draft, or Email 1 edge case)
                    if i > 0 and original_message_id:
                        result = create_outlook_reply_draft(
                            original_message_id, email.get("body", ""), to_email, email.get("subject", ""))
                    else:
                        result = create_outlook_draft(
                            to_email, email.get("subject", ""), email.get("body", ""))
                    if result.get("success"):
                        status["status"] = "draft_created"
                        status["draft_id"] = result["draft_id"]
                        status["draft_created_at"] = now.isoformat()
                        notify_telegram(
                            f"Follow-up draft ready: {company} Email {i+1}\n"
                            f"To: {to_email}\nThreaded: {'Yes' if i > 0 and original_message_id else 'No'}\n"
                            f"Check Outlook drafts to send."
                        )
                        due_count += 1
                    else:
                        status["status"] = "draft_failed"
                        status["error"] = result.get("error", "")

                email_status[key] = status

        data["email_status"] = email_status
        save_sequence(slug, data)

    if due_count == 0:
        print("No follow-ups due.")
    else:
        print(f"\n{due_count} follow-up(s) {'sent' if send_mode else 'drafted'}.")


def show_status():
    """Show all sequence statuses."""
    sequences = load_sequences()
    if not sequences:
        print("No sequences.")
        return

    by_status = {}
    for slug, data in sequences.items():
        status = data.get("status", "?")
        by_status.setdefault(status, []).append(data)

    for status, items in sorted(by_status.items()):
        print(f"\n{status.upper()} ({len(items)}):")
        for data in items:
            contact = data.get("contact", {})
            company = data.get("company", "?")
            email = contact.get("email", "?") if contact else "?"

            detail = ""
            if status == "active":
                es = data.get("email_status", {})
                sent = sum(1 for v in es.values() if v.get("status") in ("sent", "draft_created"))
                total = len(es)
                detail = f" [{sent}/{total} sent]"
            elif status == "skipped":
                cooldown = data.get("cooldown_until", "?")[:10]
                detail = f" [cooldown until {cooldown}]"

            print(f"  • {company} -> {email}{detail}")


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SEQUENCE_DIR.mkdir(parents=True, exist_ok=True)

    if "--approve" in sys.argv:
        idx = sys.argv.index("--approve")
        company = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        if company:
            approve_prospect(company)
        return

    if "--skip" in sys.argv:
        idx = sys.argv.index("--skip")
        company = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        if company:
            skip_prospect(company)
        return

    if "--stop" in sys.argv:
        idx = sys.argv.index("--stop")
        company = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        if company:
            stop_sequence(company)
        return

    if "--check" in sys.argv:
        # Outbound follow-ups AUTO-SEND by default (Hank's setting 2026-06-09), like inbound.
        # Pass --draft to fall back to creating Outlook drafts for manual send.
        send_mode = "--draft" not in sys.argv
        check_due_followups(send_mode=send_mode)
        return

    if "--status" in sys.argv:
        show_status()
        return

    print("Usage:")
    print("  outbound-sequence.py --check              # Create drafts for due follow-ups")
    print("  outbound-sequence.py --approve 'Company'  # Approve and schedule sequence")
    print("  outbound-sequence.py --skip 'Company'     # Skip with 30-day cooldown")
    print("  outbound-sequence.py --stop 'Company'     # Stop sequence (got reply)")
    print("  outbound-sequence.py --status              # Show all sequences")


if __name__ == "__main__":
    main()
