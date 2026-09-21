#!/usr/bin/env python3
"""inbound-action-slack.py — Handle APPROVE/EDIT/HANDLE LATER/SKIP for Slack-posted inbound leads.

Called by rudy-slack-listener.py button handlers. Loads the pending file for a
given Slack message ts, performs the action, and (for APPROVE/EDIT) sends the
email via Microsoft Graph as HTML with Hank's signature appended, then logs an
SF Task.

Stdout: a single JSON line describing the result (the listener parses this and
replies to the Slack thread accordingly).

Usage:
  inbound-action-slack.py --action approve --ts <slack_ts>
  inbound-action-slack.py --action edit --ts <slack_ts> --new-subject "..." --new-body-file /tmp/edited.txt
  inbound-action-slack.py --action later --ts <slack_ts>
  inbound-action-slack.py --action skip --ts <slack_ts>
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

PENDING_DIR = Path.home() / ".openclaw" / "workspace" / "pending-inbound-slack"
INBOUND_SEQ_DIR = Path.home() / ".openclaw" / "workspace" / "inbound-v2" / "sequences"
LOG = Path.home() / ".openclaw" / "logs" / "inbound-action-slack.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
GRAPH = "https://graph.microsoft.com/v1.0/me"

SIG_HTML = (
    '<span style="font-family:Aptos,sans-serif;font-size:11pt;">'
    '<a href="mailto:you@example.com" style="color:#0078D4;font-weight:bold;text-decoration:none;">Hank Autrey</a><br>'
    '<b>Sr. Business Development, datacenterHawk</b><br>'
    '555-555-5555 | datacenterhawk.com<br>'
    '14785 Preston Rd., Suite 470 | Dallas, TX 75254'
    '</span>'
)


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


def strip_plaintext_signature(body):
    """If the body ends with a plain-text version of Hank's signature, strip it
    so we don't double up when we append SIG_HTML."""
    if not body:
        return body
    idx = body.rfind("Hank Autrey")
    if idx == -1:
        return body
    if len(body) - idx <= 400:
        return body[:idx].rstrip()
    return body


def plain_to_html(body_plain):
    cleaned = strip_plaintext_signature(body_plain).strip()
    paragraphs = cleaned.split("\n\n")
    body_parts = [
        f"<p style='font-family:Aptos,sans-serif;font-size:11pt;'>{p.replace(chr(10), '<br>')}</p>"
        for p in paragraphs if p.strip()
    ]
    return "".join(body_parts) + SIG_HTML


def get_outlook_token():
    res = subprocess.run(
        ["bash", "-lc", "outlook-graph-auth.sh token"],
        capture_output=True, text=True, timeout=30,
    )
    token = res.stdout.strip()
    if not token or "ERROR" in token.upper():
        return None
    return token


def send_via_graph(to_email, subject, body_plain):
    token = get_outlook_token()
    if not token:
        return False, "could not get Outlook token"
    body_html = plain_to_html(body_plain)
    msg = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": to_email}}],
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


def sf_create_task(who_id, subject, description):
    env = os.environ.copy()
    env["SF_USE_GENERIC_UNIX_KEYCHAIN"] = "true"
    body = json.dumps({
        "Subject": subject,
        "WhoId": who_id,
        "TaskSubtype": "Email",
        "Status": "Completed",
        "Description": description,
    })
    res = subprocess.run(
        ["bash", "-lc",
         f"sf api request rest /services/data/v62.0/sobjects/Task -X POST -b '{body}' -o you@example.com"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    if res.returncode != 0:
        return False, res.stderr.strip()[:200] or res.stdout.strip()[:200]
    try:
        out = json.loads(res.stdout)
        if isinstance(out, dict) and out.get("success") is False:
            return False, str(out.get("errors", out))[:200]
    except json.JSONDecodeError:
        pass
    return True, "ok"


def sf_create_followup_task(who_id, subject, description):
    env = os.environ.copy()
    env["SF_USE_GENERIC_UNIX_KEYCHAIN"] = "true"
    body = json.dumps({
        "Subject": subject,
        "WhoId": who_id,
        "Status": "Not Started",
        "Priority": "High",
        "Description": description,
        "ActivityDate": datetime.now(timezone.utc).date().isoformat(),
    })
    res = subprocess.run(
        ["bash", "-lc",
         f"sf api request rest /services/data/v62.0/sobjects/Task -X POST -b '{body}' -o you@example.com"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    if res.returncode != 0:
        return False, res.stderr.strip()[:200] or res.stdout.strip()[:200]
    return True, "ok"


def _first_name(name, body):
    """Best-effort first name: lead_name's first token, else the greeting in the body."""
    if name:
        tok = name.strip().split()
        if tok and tok[0].lower() not in ("", "there"):
            return tok[0]
    m = re.match(r"\s*([A-Za-z][\w'.-]*)\s*[-–]", body or "")
    return m.group(1) if m else "there"


def write_inbound_sequence(draft, subject, body):
    """After Email 1 is sent, write an ACTIVE sequence file so inbound-sequence.py
    auto-fires Email 2 (Day 4) and Email 3 (Day 9) as threaded replies — unless the
    lead replies first (reply detection stops it). Standard inbound follow-up copy,
    approved 2026-06-01. Best-effort: never blocks the send/approve path."""
    try:
        to = draft["to"]
        first = _first_name(draft.get("lead_name", ""), body)
        now = datetime.now(timezone.utc)
        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

        followups = [
            (2, 4, f"{first} -\n\nMaking sure you saw this. Can I send you some times to connect?"),
            (3, 9, f"{first} -\n\nDoing a final check-in here. Would still love to show you the "
                   f"platform if you're open to it. Want to find a time?"),
        ]
        emails = []
        email_status = {"email_1": {"status": "sent", "sent_at": now.isoformat(), "send_day": 0}}
        for n, day, text in followups:
            emails.append({"n": n, "send_day": day, "subject": reply_subject, "body": text})
            email_status[f"email_{n}"] = {
                "status": "scheduled", "send_day": day,
                "scheduled_for": (now + timedelta(days=day)).isoformat(),
            }

        seq = {
            "lead_name": draft.get("lead_name", to),
            "first_name": first,
            "email": to,
            "company": draft.get("company", ""),
            "sf_id": draft.get("sf_id", ""),
            "original_subject": subject,
            "emails": emails,
            "email_status": email_status,
            "status": "active",
            "approved_at": now.isoformat(),
            "sent_date": now.isoformat(),
            "source": "inbound",
        }
        INBOUND_SEQ_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r'[^a-z0-9]+', '_', to.lower()).strip('_')
        (INBOUND_SEQ_DIR / f"{slug}.json").write_text(json.dumps(seq, indent=2, default=str))
        log(f"  inbound sequence active: {slug} (Email 2 +4d, Email 3 +9d)")
        return True
    except Exception as e:
        log(f"  warn: inbound sequence write failed: {e}")
        return False


def do_approve(filepath, draft, override_subject=None, override_body=None):
    subject = override_subject or draft["subject"]
    body = override_body if override_body is not None else draft["body"]
    to = draft["to"]
    name = draft.get("lead_name", to)

    log(f"INBOUND APPROVE slack ts={draft.get('slack_message_ts')} → {name} <{to}>")
    ok, detail = send_via_graph(to, subject, body)
    if not ok:
        log(f"  Graph send FAILED: {detail}")
        emit("error", action="approve", error=f"Graph send failed: {detail}", lead_name=name)
        return 1

    sf_id = draft.get("sf_id")
    if sf_id and len(sf_id) >= 15:
        sf_ok, sf_detail = sf_create_task(
            sf_id,
            f"Inbound reply: {subject}",
            "Sent via inbound pipeline (rudy-slack-listener, edited)" if override_body else "Sent via inbound pipeline (rudy-slack-listener)",
        )
    else:
        sf_ok, sf_detail = False, "no_sf_id"

    seq_active = write_inbound_sequence(draft, subject, body)

    filepath.unlink(missing_ok=True)
    log(f"  Email sent; sf_task={sf_detail}; sequence_active={seq_active}")
    emit("ok", action="approve", lead_name=name, to=to,
         sf_task=sf_ok, sf_detail=sf_detail, sequence_active=seq_active,
         edited=bool(override_body or override_subject))
    return 0


def do_skip(filepath, draft):
    name = draft.get("lead_name", draft.get("to", ""))
    log(f"INBOUND SKIP slack ts={draft.get('slack_message_ts')} → {name}")
    filepath.unlink(missing_ok=True)
    emit("ok", action="skip", lead_name=name)
    return 0


def do_later(filepath, draft):
    name = draft.get("lead_name", draft.get("to", ""))
    sf_id = draft.get("sf_id")
    log(f"INBOUND LATER slack ts={draft.get('slack_message_ts')} → {name}")

    if sf_id and len(sf_id) >= 15:
        sf_ok, sf_detail = sf_create_followup_task(
            sf_id,
            f"Inbound lead — needs reply: {draft.get('subject', '')}",
            f"Deferred via Slack Rudy. Draft: {draft.get('body', '')[:1000]}",
        )
    else:
        sf_ok, sf_detail = False, "no_sf_id"

    draft["status"] = "deferred"
    draft["deferred_at"] = datetime.now(timezone.utc).isoformat()
    filepath.write_text(json.dumps(draft, indent=2))
    emit("ok", action="later", lead_name=name, sf_task=sf_ok, sf_detail=sf_detail)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", required=True, choices=["approve", "edit", "later", "skip"])
    ap.add_argument("--ts", required=True, help="Slack message ts of the card")
    ap.add_argument("--new-subject")
    ap.add_argument("--new-body")
    ap.add_argument("--new-body-file")
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
    if args.action == "later":
        return do_later(filepath, draft)
    if args.action == "skip":
        return do_skip(filepath, draft)


if __name__ == "__main__":
    sys.exit(main())
