#!/usr/bin/env python3
"""outbound-action-slack.py — Handle APPROVE / EDIT (direct) / EDIT (Opus) / SKIP for Slack-posted outbound cards.

Called by rudy-slack-listener.py button handlers. Loads the pending file for a
given Slack message ts, performs the action.

APPROVE flow mirrors telegram-responder.py:do_approve — create SF Account (if
CLEAR_NEW), find/create Contact, send email via Graph, create Task, build the
sequence file so outbound-sequence.py can fire Email 2/3, update cooldown state,
delete pending.

EDIT (direct) — caller sends new_subject + new_body_file; we run APPROVE with
those overrides.

EDIT (Opus) — caller sends instructions; we call Opus to revise the draft,
update the pending file, and update (in place) the Block Kit card on Slack with
the new draft. No email sent.

SKIP — mark cooldown, delete pending.

Stdout: single JSON line { "status": "ok"|"error", ... } for the listener.
"""
import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

CRED_FILE = Path.home() / ".openclaw" / "slack-rudy.json"
PENDING_DIR = Path.home() / ".openclaw" / "workspace" / "pending-outbound-slack"
LOG = Path.home() / ".openclaw" / "logs" / "outbound-action-slack.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
GRAPH = "https://graph.microsoft.com/v1.0/me"

VAULT_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/hank-vault/outbound-v2"
LOCAL_DIR = Path.home() / ".openclaw/workspace/outbound-v2"
VAULT_SEQ_DIR = VAULT_DIR / "sequences"
LOCAL_SEQ_DIR = LOCAL_DIR / "sequences"
VAULT_RESULTS = VAULT_DIR / "pipeline-results.json"
LOCAL_RESULTS = LOCAL_DIR / "pipeline-results.json"
SENT_SEQ_DIR = LOCAL_DIR / "sent-sequences"
STATE_FILE = LOCAL_DIR / "state.json"


def log(msg):
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}\n"
    with LOG.open("a") as f:
        f.write(line)


def emit(status, **kwargs):
    print(json.dumps({"status": status, **kwargs}))


def load_pending(ts):
    f = PENDING_DIR / f"{ts}.json"
    if not f.exists():
        return None, None
    return f, json.loads(f.read_text())


def save_pending_in_place(filepath, draft):
    tmp = filepath.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(draft, indent=2, default=str))
    tmp.replace(filepath)


def _load_anthropic_key():
    for p in [Path.home() / ".openclaw/config/api-keys.md",
              Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/hank-vault/config/api-keys.md"]:
        try:
            if p.exists():
                m = re.search(r"## Anthropic[\s\S]*?API Key \(DCH Company\)[^`]*`([^`]+)`", p.read_text())
                if m:
                    return m.group(1).strip()
        except (PermissionError, OSError):
            continue
    return None


# ---------- Graph send ----------

def get_outlook_token():
    res = subprocess.run(["bash", "-lc", "outlook-graph-auth.sh token"],
                         capture_output=True, text=True, timeout=30)
    token = res.stdout.strip()
    if not token or "ERROR" in token.upper():
        return None
    return token


def send_email_graph(to_email, to_name, subject, body_html):
    token = get_outlook_token()
    if not token:
        return False, "could not get Outlook token"
    msg = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": to_email, "name": to_name or to_email}}],
            "categories": ["Rudy-Outbound"],
        },
        "saveToSentItems": True,
    }
    res = subprocess.run(
        ["curl", "-s", "-w", "\n%{http_code}", "-X", "POST",
         f"{GRAPH}/sendMail",
         "-H", f"Authorization: Bearer {token}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(msg)],
        capture_output=True, text=True, timeout=30,
    )
    lines = res.stdout.strip().split("\n")
    http_code = lines[-1] if lines else ""
    if http_code not in ("200", "202", "204"):
        body = "\n".join(lines[:-1])[:200]
        return False, f"Graph HTTP {http_code}: {body}"
    return True, "sent"


# ---------- SF helpers (sf CLI) ----------

def sf_query(soql):
    env = os.environ.copy()
    env["SF_USE_GENERIC_UNIX_KEYCHAIN"] = "true"
    res = subprocess.run(
        ["bash", "-lc",
         f"sf data query --query \"{soql}\" --target-org you@example.com --json"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    if res.returncode != 0:
        return None
    stdout = res.stdout
    candidates = [i for i in (stdout.find("{"), stdout.find("[")) if i >= 0]
    json_text = stdout[min(candidates):] if candidates else stdout
    try:
        return json.loads(json_text).get("result", {})
    except json.JSONDecodeError:
        return None


def sf_create(sobject, fields):
    env = os.environ.copy()
    env["SF_USE_GENERIC_UNIX_KEYCHAIN"] = "true"
    body = json.dumps(fields)
    res = subprocess.run(
        ["bash", "-lc",
         f"sf api request rest /services/data/v62.0/sobjects/{sobject} -X POST -b '{body}' -o you@example.com"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    if res.returncode != 0:
        return None, (res.stderr.strip() or res.stdout.strip())[:300]
    # sf CLI emits update-available warnings to stdout BEFORE the JSON response.
    # Find the start of the JSON ({ or [) and parse from there.
    stdout = res.stdout
    candidates = [i for i in (stdout.find("{"), stdout.find("[")) if i >= 0]
    json_text = stdout[min(candidates):] if candidates else stdout
    try:
        out = json.loads(json_text)
        if isinstance(out, dict) and out.get("id"):
            return out["id"], None
        if isinstance(out, list) and out and out[0].get("errorCode"):
            return None, str(out)[:300]
        return None, str(out)[:300]
    except json.JSONDecodeError:
        return None, res.stdout[:300]


# ---------- State + sequences ----------

def update_state(company, status):
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            state = {}
    state[company.lower()] = {
        "status": status,
        "last_contact": datetime.now(timezone.utc).isoformat(),
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def build_and_write_sequence(company, email, name, subject, body_plain, body_html, draft):
    now = datetime.now(timezone.utc)
    slug = re.sub(r'[^a-z0-9]+', '-', company.lower()).strip('-')

    # Load all 3 emails from pipeline-results.json for Email 2/3 firing
    all_emails = []
    for pf in [VAULT_RESULTS, LOCAL_RESULTS]:
        if pf.exists():
            try:
                pstate = json.loads(pf.read_text())
                prospect_rec = pstate.get(company.lower().strip()) or {}
                em = (prospect_rec.get("emails") or {}).get("emails") or prospect_rec.get("emails") or []
                if isinstance(em, list) and em:
                    all_emails = em
                    break
            except Exception as e:
                log(f"  warn: reading {pf}: {e}")

    if not all_emails:
        all_emails = [{"subject": subject, "body": body_plain, "send_day": 0}]
        log(f"  warn: no emails list for {company} — sequence will have Email 1 only")

    email_status = {}
    for i, em in enumerate(all_emails):
        send_day = em.get("send_day", 0 if i == 0 else (4 if i == 1 else 10))
        if i == 0:
            email_status[f"email_{i+1}"] = {"status": "sent", "sent_at": now.isoformat(), "send_day": 0}
        else:
            scheduled_for = (now + timedelta(days=send_day)).isoformat()
            email_status[f"email_{i+1}"] = {
                "status": "scheduled", "send_day": send_day,
                "scheduled_for": scheduled_for, "draft_id": None,
            }

    seq_data = {
        "company": company, "domain": draft.get("domain"),
        "contact": {"email": email, "name": name, "title": draft.get("contact_title", "")},
        "emails": all_emails, "email_status": email_status,
        "status": "active", "approved_at": now.isoformat(),
        "sent_date": now.isoformat(), "created": draft.get("created"),
        "created_from": "rudy-slack-listener",
        "sf_account_id": draft.get("sf_account_id"),
        "original_body_html": body_html,
    }

    # Write LOCAL workspace dir FIRST (this is the only dir outbound-sequence.py --check
    # reads under launchd/SSH, where iCloud TCC blocks reads). Then write the iCloud vault
    # as a best-effort cross-machine copy. Do NOT break after the first success — the old
    # code wrote iCloud-first-then-break, so the active sequence never reached the workspace
    # dir and follow-ups never fired.
    target_dir = None
    for d in [LOCAL_SEQ_DIR, VAULT_SEQ_DIR]:
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{slug}.json").write_text(json.dumps(seq_data, indent=2, default=str))
            if target_dir is None:
                target_dir = d
        except Exception as e:
            log(f"  warn: sequence write to {d} failed: {e}")

    try:
        SENT_SEQ_DIR.mkdir(parents=True, exist_ok=True)
        (SENT_SEQ_DIR / f"{slug}.json").write_text(json.dumps({
            "company": company, "domain": draft.get("domain"), "contact_name": name,
            "email": email, "subject": subject, "body": body_plain,
            "sent_date": now.isoformat(), "created_from": "rudy-slack-listener",
        }, indent=2))
    except Exception as e:
        log(f"  warn: legacy sent-sequences write failed: {e}")

    return target_dir, email_status


# ---------- HTML body rebuild ----------

SIG_HTML = (
    '<br><br>'
    '<span style="font-family:Aptos,sans-serif;font-size:11pt;">'
    '<a href="mailto:you@example.com" style="color:#0078D4;font-weight:bold;text-decoration:none;">Hank Autrey</a><br>'
    '<b>Sr. Business Development, datacenterHawk</b><br>'
    '555-555-5555 | datacenterhawk.com<br>'
    '14785 Preston Rd., Suite 470 | Dallas, TX 75254'
    '</span>'
)


def rebuild_html_body(plain_body):
    import html as html_lib
    body = plain_body.rstrip()
    for tail in ("\nHank Autrey", "\n\nHank", "\nHank"):
        if body.endswith(tail):
            body = body[: -len(tail)].rstrip()
            break
    return ('<div style="font-family:Aptos,sans-serif;font-size:11pt;">'
            + html_lib.escape(body).replace("\n", "<br>") + SIG_HTML + '</div>')


# ---------- Opus revise (edit via instructions) ----------

def call_opus_revise(draft, instructions):
    key = _load_anthropic_key()
    if not key:
        return None, "no Anthropic key"

    prompt = (
        "You are revising an outbound email draft for Hank Autrey at datacenterHawk.\n\n"
        f"To: {draft.get('contact_name')} ({draft.get('contact_email')})\n"
        f"Subject: {draft.get('subject')}\n\n"
        f"{draft.get('body_plain', '')}\n\n"
        "---\n"
        f"Hank's edit instructions: {instructions}\n\n"
        "Revise the draft following his instructions AND these rules: casual tone, "
        "contractions, short sentences (8-15 words), 50-100 words total, regular hyphens "
        "only (never em-dashes/en-dashes), always end with 'Can I send you some times to "
        "connect?' or 'Would you be open to taking a look?', never say 'I'm Hank with "
        "datacenterHawk', never use 'datacenterHawk is' (use 'We are'), no corporate jargon, NEVER use hedging-ownership phrases like 'kind of our thing'/'sort of our thing'/'kinda our thing'/'kind of what we do' (use direct statements: 'That's what we do', 'That's exactly what we cover', 'We track that'), "
        "no bullet points, no bold formatting.\n\n"
        "Output ONLY the revised email body (plain text, no subject line, no signature)."
    )

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({"model": "claude-opus-4-8", "max_tokens": 1500,
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            method="POST",
        )
        ctx = ssl.create_default_context()
        resp = urllib.request.urlopen(req, context=ctx, timeout=60)
        result = json.loads(resp.read())
        text = (result.get("content") or [{}])[0].get("text", "").strip()
        return text or None, None
    except Exception as e:
        return None, str(e)


# ---------- Slack update card in place ----------

def slack_update_card(creds, channel, ts, draft):
    """Re-render the Block Kit card with updated body and chat.update it in place."""
    company = draft.get("company", "?")
    body = draft.get("body_plain", "")
    body_preview = body if len(body) <= 1200 else body[:1200] + "..."
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🎯 OUTBOUND — {company} (revised)"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Contact:*\n{draft.get('contact_name','?')}"},
                {"type": "mrkdwn", "text": f"*Email:*\n{draft.get('contact_email','?')}"},
            ],
        },
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Subject:* {draft.get('subject','')}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{body_preview}```"}},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✓ Approve & Send"},
                 "style": "primary", "action_id": "outbound_approve"},
                {"type": "button", "text": {"type": "plain_text", "text": "✎ Edit"},
                 "action_id": "outbound_edit"},
                {"type": "button", "text": {"type": "plain_text", "text": "✏️ Ask Rudy to Edit"},
                 "action_id": "outbound_edit_opus"},
                {"type": "button", "text": {"type": "plain_text", "text": "🗑 Skip"},
                 "style": "danger", "action_id": "outbound_skip"},
            ],
        },
    ]
    payload = json.dumps({
        "channel": channel, "ts": ts, "blocks": blocks,
        "text": f"OUTBOUND PROSPECT — {company} (revised)",
    }).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.update",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {creds['bot_token']}"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=20)
    return json.loads(resp.read())


# ---------- Actions ----------

# Send-time guard. The outbound voice fixes only change FUTURE drafts — a draft that was
# carded BEFORE a fix still carries the old salesy formula, and approving it would send it.
# This blocks the send (forcing a regen/edit) if the body or subject still contains a banned
# pain-assertion pattern. See feedback_hank_outbound_voice. Hank can fix via EDIT.
_BANNED_SEND_PATTERNS = [
    "i imagine",
    "with you leading",
    "exactly what we cover",
    "especially top of mind",
    "especially important to you",
    "real friction point",
]


def formula_violation(*texts):
    blob = " ".join(t for t in texts if t).lower()
    return [p for p in _BANNED_SEND_PATTERNS if p in blob]


def do_approve(filepath, draft, override_subject=None, override_body=None):
    company = draft["company"]
    email = draft["contact_email"]
    name = draft.get("contact_name") or email
    subject = override_subject or draft["subject"]

    if override_body is not None:
        plain_body = override_body
        body_html = rebuild_html_body(plain_body)
    else:
        plain_body = draft.get("body_plain", "")
        body_html = draft.get("body_html") or rebuild_html_body(plain_body)

    log(f"APPROVE: {company} → {email} (edited={bool(override_body or override_subject)})")

    # Send-time guard — block any draft (stale pre-fix or edited) that still carries the
    # banned salesy formula. Runs BEFORE any SF writes or the Graph send, so a blocked
    # send creates nothing. Hank fixes it by tapping EDIT and rewriting.
    hits = formula_violation(plain_body, subject)
    if hits:
        emit("blocked", error=("draft still contains banned salesy pattern(s): "
             + ", ".join(hits) + " — regenerate or edit before sending."), company=company)
        log(f"  BLOCKED send for {company}: banned pattern(s) {hits}")
        return 1

    # Step 1: SF Account (if CLEAR_NEW + missing)
    account_id = draft.get("sf_account_id")
    if not account_id and draft.get("roe_status") == "CLEAR_NEW":
        domain = draft.get("domain", "")
        name_escaped = company.replace("'", "\\'")
        clauses = [f"Name = '{name_escaped}'"]
        if domain:
            d_esc = domain.replace("'", "\\'")
            clauses.append(f"Website LIKE '%{d_esc}%'")
        existing = sf_query(f"SELECT Id, Name FROM Account WHERE {' OR '.join(clauses)} LIMIT 1")
        if existing and existing.get("records"):
            account_id = existing["records"][0]["Id"]
            log(f"  Account dup found, reusing: {account_id}")
        else:
            sf_type = draft.get("sf_type")
            sf_subtype = draft.get("sf_subtype")
            if not sf_type or not sf_subtype:
                emit("error", error=f"missing sf_type/sf_subtype for {company}", company=company)
                return 1
            region = draft.get("sf_region") or "North America"
            account_id, err = sf_create("Account", {
                "Name": company, "Website": f"https://{domain}" if domain else "",
                "Type": sf_type, "Account_Subtype_New__c": sf_subtype, "Region__c": region,
            })
            if not account_id:
                emit("error", error=f"SF Account create failed: {err}", company=company)
                return 1
            log(f"  Created Account: {account_id}")

    # Step 2: SF Contact (find or create)
    contact_id = None
    if account_id:
        ex_contact = sf_query(f"SELECT Id, Name FROM Contact WHERE Email = '{email}' LIMIT 1")
        if ex_contact and ex_contact.get("records"):
            contact_id = ex_contact["records"][0]["Id"]
            log(f"  Contact exists, reusing: {contact_id}")
        else:
            ex_lead = sf_query(f"SELECT Id FROM Lead WHERE Email = '{email}'")
            if ex_lead and ex_lead.get("records"):
                log(f"  Lead exists for {email}, skipping Contact create")
            else:
                first = name.split()[0] if name else ""
                last = " ".join(name.split()[1:]) if name and len(name.split()) > 1 else name
                contact_id, err = sf_create("Contact", {
                    "FirstName": first, "LastName": last, "Email": email,
                    "Title": draft.get("contact_title", ""), "AccountId": account_id,
                })
                if not contact_id:
                    emit("error", error=f"SF Contact create failed: {err}", company=company)
                    return 1
                log(f"  Created Contact: {contact_id}")

    # Step 3: send via Graph
    ok, detail = send_email_graph(email, name, subject, body_html)
    if not ok:
        emit("error", error=f"Graph send failed: {detail}", company=company)
        return 1

    # Step 4: SF Task — removed 2026-05-18.
    # Reason: EAC auto-syncs the sent email into the SF activity timeline. Creating a
    # parallel Task here resulted in two entries per send (the EAC-synced email plus
    # an "Outbound: ..." Task with the same body). Provenance now lives on the email
    # itself via the "Rudy-Outbound" Outlook category set in send_email_graph(), which
    # is preserved through the EAC sync and lets us filter pipeline-originated sends.
    # Backup of the prior behavior: outbound-action-slack.py.bak-no-dup-task-20260518.

    # Step 5: sequence files
    seq_dir, email_status = build_and_write_sequence(
        company, email, name, subject, plain_body, body_html, draft,
    )

    # Step 6: cooldown + cleanup
    update_state(company, "sent")
    filepath.unlink(missing_ok=True)

    e2 = email_status.get("email_2", {}).get("send_day")
    e3 = email_status.get("email_3", {}).get("send_day")
    emit("ok", action="approve", company=company, to=email,
         account_id=account_id, contact_id=contact_id,
         seq_dir=str(seq_dir) if seq_dir else None,
         email_2_day=e2, email_3_day=e3,
         edited=bool(override_body or override_subject))
    return 0


def do_edit_opus(filepath, draft, instructions, creds, channel, ts):
    company = draft["company"]
    log(f"EDIT_OPUS: {company} ← {instructions[:80]}")

    revised, err = call_opus_revise(draft, instructions)
    if not revised:
        emit("error", action="edit_opus", error=f"Opus revise failed: {err}", company=company)
        return 1

    draft["body_plain"] = revised
    draft["body_html"] = rebuild_html_body(revised)
    draft["last_edited"] = datetime.now(timezone.utc).isoformat()
    draft["edit_count"] = draft.get("edit_count", 0) + 1
    save_pending_in_place(filepath, draft)

    update_result = slack_update_card(creds, channel, ts, draft)
    if not update_result.get("ok"):
        log(f"  chat.update failed: {update_result}")

    emit("ok", action="edit_opus", company=company,
         edit_count=draft["edit_count"], revised_preview=revised[:200])
    return 0


def do_skip(filepath, draft):
    company = draft["company"]
    log(f"SKIP: {company}")
    update_state(company, "skipped")
    filepath.unlink(missing_ok=True)
    emit("ok", action="skip", company=company)
    return 0


# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", required=True, choices=["approve", "edit", "edit_opus", "skip"])
    ap.add_argument("--ts", required=True)
    ap.add_argument("--new-subject")
    ap.add_argument("--new-body")
    ap.add_argument("--new-body-file")
    ap.add_argument("--instructions", help="for edit_opus")
    args = ap.parse_args()

    filepath, draft = load_pending(args.ts)
    if draft is None:
        emit("error", error=f"no pending file for ts={args.ts}")
        return 1

    if args.action == "approve":
        return do_approve(filepath, draft)
    if args.action == "edit":
        new_body = None
        if args.new_body_file:
            new_body = Path(args.new_body_file).read_text()
        elif args.new_body:
            new_body = args.new_body
        return do_approve(filepath, draft, override_subject=args.new_subject, override_body=new_body)
    if args.action == "edit_opus":
        if not args.instructions:
            emit("error", error="edit_opus requires --instructions")
            return 1
        creds = json.loads(CRED_FILE.read_text())
        channel = draft.get("slack_channel") or creds["channels"]["rudy_outbound"]
        return do_edit_opus(filepath, draft, args.instructions, creds, channel, args.ts)
    if args.action == "skip":
        return do_skip(filepath, draft)


if __name__ == "__main__":
    sys.exit(main())
