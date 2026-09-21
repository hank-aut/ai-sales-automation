import os
#!/usr/bin/env python3
"""inbound-sequence.py — Inbound follow-up engine (Email 2 @ Day 4, Email 3 @ Day 9).

When an inbound lead card is APPROVED in Slack, inbound-action-slack.py sends Email 1
and writes an *active* sequence file here. This script fires the follow-ups:

  - Email 2 (Day 4): "Making sure you saw this..."
  - Email 3 (Day 9): "Doing a final check-in here..."

Follow-ups AUTO-SEND (threaded replies on the original subject) UNLESS the lead has
replied — reply detection stops the sequence. Mirrors the outbound engine's Graph/
threading/reply machinery, but sends instead of drafting.

Usage:
  inbound-sequence.py --check     # fire any due follow-ups (cron entrypoint)
  inbound-sequence.py --status    # show all sequence statuses
  inbound-sequence.py --stop <email-or-slug>   # manually stop a sequence
"""
import argparse
import json
import subprocess
import sys
import html
import fcntl
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

# email_safety lives in ~/.local/bin alongside this script
sys.path.insert(0, str(Path.home() / ".local" / "bin"))
try:
    from email_safety import validate_email_before_send
except Exception:
    def validate_email_before_send(subject, body, to_email):
        if not body or not body.strip():
            return False, "empty body"
        return True, ""
try:
    from reply_classify import detect_departure
except Exception:
    def detect_departure(text):
        return False, []

SEQUENCE_DIR = Path.home() / ".openclaw" / "workspace" / "inbound-v2" / "sequences"
LOCK_FILE = Path.home() / ".openclaw" / "workspace" / "inbound-v2" / ".sequence-check.lock"
LAST_CHECK_FILE = Path.home() / ".openclaw" / "workspace" / "inbound-v2" / ".last-sequence-check"
LOG_FILE = Path.home() / ".openclaw" / "logs" / "inbound-sequence.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

LOCAL_PATH = "export PATH=$HOME/.local/bin:$HOME/.nvm/versions/node/v22.22.2/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
MIN_CHECK_INTERVAL_SECONDS = 3600
SLACK_INBOUND_CHANNEL = os.environ.get("SLACK_INBOUND_CHANNEL", "")  # #rudy-inbound

SIGNATURE_HTML = (
    '<span style="font-family:Aptos,sans-serif;font-size:11pt;">'
    '<a href="mailto:you@example.com" style="color:#0078D4;font-weight:bold;text-decoration:none;">Hank Autrey</a><br>'
    '<b>Sr. Business Development, datacenterHawk</b><br>'
    '555-555-5555 | datacenterhawk.com<br>'
    '14785 Preston Rd., Suite 470 | Dallas, TX 75254'
    '</span>'
)


def log(msg):
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}\n"
    with LOG_FILE.open("a") as f:
        f.write(line)


def body_to_html(body_text):
    escaped = html.escape(body_text)
    html_body = escaped.replace("\n", "<br>")
    return (
        '<html><body>'
        '<div style="font-family:Aptos,sans-serif;font-size:11pt;">'
        f'{html_body}'
        '</div><br>'
        f'{SIGNATURE_HTML}'
        '</body></html>'
    )


def slug_for(email):
    return re.sub(r'[^a-z0-9]+', '_', (email or "").lower()).strip('_')


# ---------- Graph helpers (run locally on the OpenClaw remote) ----------

def graph_api_call(method, endpoint, payload=None):
    base_cmd = (
        f"{LOCAL_PATH} && "
        f"TOKEN=$(outlook-graph-auth.sh token) && "
        f"curl -s -X {method} 'https://graph.microsoft.com/v1.0{endpoint}' "
        f"-H \"Authorization: Bearer $TOKEN\""
    )
    if payload is not None:
        payload_json = json.dumps(payload) if isinstance(payload, dict) else payload
        cmd = base_cmd + " -H 'Content-Type: application/json' -d @-"
        result = subprocess.run(
            ["bash", "-c", cmd],
            input=payload_json.encode("utf-8"),
            capture_output=True, timeout=30,
        )
    else:
        result = subprocess.run(["bash", "-c", base_cmd], capture_output=True, timeout=30)
    stdout = result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout
    return json.loads(stdout) if stdout.strip() else {}


def find_sent_message_id(to_email, subject, sent_after=None):
    """Find the original sent message in Outlook Sent Items by recipient + subject.

    The OData `subject eq '...' and toRecipients/any(...)` filter is unreliable (it
    silently returns empty when the subject contains '+', e.g. "datacenterHawk + X").
    Instead bound by sentDateTime and match subject + recipient in Python. With a floor
    we scan ASCENDING (Email 1 is the oldest at/after the floor); without, DESCENDING."""
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
                return {"id": msg["id"], "conversationId": msg.get("conversationId", ""),
                        "sentDateTime": msg.get("sentDateTime", "")}
    except Exception as e:
        log(f"find_sent_message_id error: {e}")
    return None


def _reply_fragment(body_text):
    """My new reply text + signature as an HTML fragment, to sit ABOVE the quoted history."""
    safe = html.escape(body_text).replace("\n", "<br>")
    return (
        '<div style="font-family:Aptos,sans-serif;font-size:11pt;">'
        f'{safe}</div><br>{SIGNATURE_HTML}<br>'
    )


def _merge_above_quote(fragment, quoted_html):
    """Insert the new reply fragment just inside <body> of the Graph-generated quoted
    reply, so the result is: new text on top, then the quoted original below (Hank's
    real reply format). Falls back to simple prepend if no <body> tag."""
    if not quoted_html:
        return f"<html><body>{fragment}</body></html>"
    low = quoted_html.lower()
    i = low.find("<body")
    if i != -1:
        j = quoted_html.find(">", i)
        if j != -1:
            return quoted_html[:j + 1] + fragment + quoted_html[j + 1:]
    return fragment + quoted_html


def send_threaded_reply(original_message_id, body_text, to_email, subject):
    """createReply -> prepend our text above Graph's quoted history -> send.
    Threaded reply (same conversation), validated against the real recipient + subject."""
    ok, err = validate_email_before_send(subject, body_text, to_email)
    if not ok:
        return {"success": False, "error": err}
    try:
        reply_resp = graph_api_call(
            "POST", f"/me/messages/{original_message_id}/createReply", {"comment": ""})
        reply_draft_id = reply_resp.get("id", "")
        if not reply_draft_id:
            return {"success": False,
                    "error": reply_resp.get("error", {}).get("message", "createReply failed")}
        # Graph already populated the draft with the quoted original — fetch it and
        # prepend our text instead of overwriting (which would wipe the quote).
        existing = graph_api_call("GET", f"/me/messages/{reply_draft_id}?$select=body")
        quoted_html = (existing.get("body") or {}).get("content", "")
        combined = _merge_above_quote(_reply_fragment(body_text), quoted_html)
        # createReply on a message WE sent addresses the reply back to the sender (us),
        # so we MUST override toRecipients to the lead — otherwise the follow-up goes to
        # Hank instead of the prospect.
        patch_resp = graph_api_call(
            "PATCH", f"/me/messages/{reply_draft_id}",
            {"body": {"contentType": "HTML", "content": combined},
             "toRecipients": [{"emailAddress": {"address": to_email}}]})
        if not patch_resp.get("id"):
            return {"success": False,
                    "error": patch_resp.get("error", {}).get("message", "PATCH failed")}
        # /send returns 202 with empty body on success
        base_cmd = (
            f"{LOCAL_PATH} && TOKEN=$(outlook-graph-auth.sh token) && "
            f"curl -s -w '\\n%{{http_code}}' -X POST "
            f"'https://graph.microsoft.com/v1.0/me/messages/{reply_draft_id}/send' "
            f"-H \"Authorization: Bearer $TOKEN\" -H 'Content-Length: 0'"
        )
        res = subprocess.run(["bash", "-c", base_cmd], capture_output=True, text=True, timeout=30)
        code = res.stdout.strip().split("\n")[-1] if res.stdout.strip() else ""
        if code in ("200", "202", "204"):
            return {"success": True, "message_id": reply_draft_id}
        return {"success": False, "error": f"send HTTP {code}: {res.stdout[:160]}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def check_inbox_for_reply(email_address, since_iso=None):
    """Return {has_reply: bool, ...} — True if the lead has emailed us at/after
    since_iso (sequence start).

    IMPORTANT: the filter MUST be URL-encoded (raw spaces/'+'/datetime in the URL
    silently break the query → empty result → missed reply → bad auto-send). Also do
    NOT combine $filter(from) with $orderby(receivedDateTime) — Graph rejects ordering
    on a different field than the filter. So we filter by sender only and compare the
    received timestamp in Python."""
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
        # newest first
        msgs.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
        for m in msgs:
            rcv = m.get("receivedDateTime", "")
            if floor:
                # Only count messages we can CONFIRM arrived after the sequence started.
                # (Prior bug: naive-vs-aware datetime comparison threw, was swallowed, and
                #  the message counted as a reply — so ancient unrelated email false-stopped
                #  sequences. Skip anything we can't confirm is >= floor.)
                try:
                    rcv_dt = datetime.fromisoformat(rcv.replace("Z", "+00:00"))
                    if rcv_dt.tzinfo is None:
                        rcv_dt = rcv_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if rcv_dt < floor:
                    continue
            return {"has_reply": True, "subject": m.get("subject", ""),
                    "date": rcv[:19], "preview": m.get("bodyPreview", "")}
        return {"has_reply": False}
    except Exception as e:
        log(f"inbox check error {email_address}: {e}")
        return {"has_reply": False, "error": str(e)}


# ---------- Salesforce + Slack ----------

def sf_create_completed_email_task(who_id, subject):
    if not who_id or len(who_id) < 15:
        return False, "no_sf_id"
    body = json.dumps({
        "Subject": f"Inbound follow-up sent: {subject}",
        "WhoId": who_id,
        "TaskSubtype": "Email",
        "Status": "Completed",
        "Description": "Auto-sent by inbound-sequence.py follow-up engine.",
    })
    cmd = (
        f"export SF_USE_GENERIC_UNIX_KEYCHAIN=true && {LOCAL_PATH} && "
        f"sf api request rest /services/data/v62.0/sobjects/Task -X POST "
        f"-b '{body}' -o you@example.com"
    )
    res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=40)
    if res.returncode != 0:
        return False, (res.stderr or res.stdout).strip()[:160]
    return True, "ok"


def slack_notify(message):
    try:
        subprocess.run(
            [str(Path.home() / ".local/bin/slack_send.sh"), SLACK_INBOUND_CHANNEL, message],
            capture_output=True, timeout=15)
    except Exception as e:
        log(f"slack notify error: {e}")


# ---------- Sequence storage ----------

def load_sequences():
    SEQUENCE_DIR.mkdir(parents=True, exist_ok=True)
    seqs = {}
    for f in SEQUENCE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            data["_path"] = str(f)
            seqs[f.stem] = data
        except Exception:
            pass
    return seqs


def save_sequence(data):
    path = data.get("_path")
    to_write = {k: v for k, v in data.items() if k != "_path"}
    if not path:
        path = str(SEQUENCE_DIR / f"{slug_for(data.get('email'))}.json")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(to_write, indent=2, default=str))


# ---------- Commands ----------

def check():
    now = datetime.now(timezone.utc)
    # global dedup
    if LAST_CHECK_FILE.exists():
        try:
            last = datetime.fromisoformat(LAST_CHECK_FILE.read_text().strip())
            if (now - last).total_seconds() < MIN_CHECK_INTERVAL_SECONDS:
                print(f"Inbound check ran {int((now-last).total_seconds())}s ago. Skipping.")
                return
        except Exception:
            pass
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = LOCK_FILE.open("w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another inbound --check is running. Exiting.")
        return
    LAST_CHECK_FILE.write_text(now.isoformat())

    sent_count = 0
    sequences = load_sequences()
    for slug, data in sequences.items():
        if data.get("status") != "active":
            continue
        email = data.get("email")
        company = data.get("company", slug)
        first = data.get("first_name", "")
        approved_at = data.get("approved_at") or data.get("sent_date") or ""

        # 1) reply detection — stop sequence if lead replied
        reply = check_inbox_for_reply(email, approved_at if approved_at else None)
        if reply.get("has_reply"):
            data["status"] = "reply_received"
            data["reply_subject"] = reply.get("subject", "")
            data["stopped_at"] = now.isoformat()
            is_dep, repl = detect_departure(
                (reply.get("preview", "") + " " + reply.get("subject", "")))
            if is_dep:
                data["departure_detected"] = True
                rl = ("\nPossible replacement(s): " + ", ".join(repl)) if repl else ""
                slack_notify(
                    f"🚪 LEFT COMPANY — {company} ({first})\n"
                    f"{email} appears to have left. Inbound sequence stopped.{rl}\n"
                    f"Reply: \"{reply.get('preview','')[:240]}\"\n"
                    f"→ Consider pivoting to a replacement contact.")
            else:
                slack_notify(f"🟢 {company} ({first}) replied — inbound sequence stopped. "
                             f"Re: {reply.get('subject','')}")
            save_sequence(data)
            log(f"reply -> stop: {company} {email} (departure={is_dep})")
            continue

        emails = data.get("emails", [])
        estatus = data.get("email_status", {})
        original_subject = data.get("original_subject", "")
        orig_msg_id = data.get("original_message_id")

        for em in emails:
            n = em.get("n")
            key = f"email_{n}"
            st = estatus.get(key, {})
            if st.get("status") != "scheduled":
                continue
            try:
                due = datetime.fromisoformat(st.get("scheduled_for"))
            except Exception:
                continue
            if due > now:
                continue

            # need the original sent message to thread onto
            if not orig_msg_id:
                found = find_sent_message_id(
                    email, original_subject, approved_at if approved_at else None)
                if found:
                    orig_msg_id = found["id"]
                    data["original_message_id"] = orig_msg_id
                    data["conversation_id"] = found.get("conversationId", "")
            if not orig_msg_id:
                log(f"no original message for {company} — cannot thread Email {n}, skipping this run")
                st["status"] = "deferred_no_thread"
                estatus[key] = st
                continue

            result = send_threaded_reply(
                orig_msg_id, em.get("body", ""), email, em.get("subject", original_subject))
            if result.get("success"):
                st["status"] = "sent"
                st["sent_at"] = now.isoformat()
                st["message_id"] = result["message_id"]
                estatus[key] = st
                sent_count += 1
                sf_ok, sf_detail = sf_create_completed_email_task(data.get("sf_id"), original_subject)
                slack_notify(
                    f"📤 Inbound follow-up sent: {company} ({first}) — Email {n} (Day {em.get('send_day')}). "
                    f"Threaded reply. SF task: {'ok' if sf_ok else sf_detail}")
                log(f"sent Email {n} to {email} ({company})")
            else:
                st["status"] = "send_failed"
                st["error"] = result.get("error", "")
                estatus[key] = st
                slack_notify(f"⚠️ Inbound follow-up FAILED: {company} Email {n} — {result.get('error','')}")
                log(f"send failed Email {n} {company}: {result.get('error','')}")

        # mark completed when no scheduled emails remain
        data["email_status"] = estatus
        if not any(v.get("status") == "scheduled" for v in estatus.values()):
            data["status"] = "completed"
            data["completed_at"] = now.isoformat()
        save_sequence(data)

    print(f"Inbound check done. {sent_count} follow-up(s) sent." if sent_count
          else "Inbound check done. No follow-ups due.")


def show_status():
    sequences = load_sequences()
    if not sequences:
        print("No inbound sequences.")
        return
    by_status = {}
    for slug, d in sequences.items():
        by_status.setdefault(d.get("status", "?"), []).append(d)
    for status, items in sorted(by_status.items()):
        print(f"\n{status.upper()} ({len(items)}):")
        for d in items:
            es = d.get("email_status", {})
            pend = [k for k, v in es.items() if v.get("status") == "scheduled"]
            nxt = ""
            if pend:
                k = sorted(pend)[0]
                nxt = f"  next: {k} @ {es[k].get('scheduled_for','')[:10]}"
            print(f"  • {d.get('company', slug)} -> {d.get('email','')}{nxt}")


def stop_sequence(key):
    sequences = load_sequences()
    target = None
    for slug, d in sequences.items():
        if key.lower() in (slug.lower(), (d.get("email") or "").lower(),
                            (d.get("company") or "").lower()):
            target = d
            break
    if not target:
        print(f"No sequence matching '{key}'. Available: "
              + ", ".join(d.get("company", s) for s, d in sequences.items()))
        return
    target["status"] = "stopped"
    target["stopped_at"] = datetime.now(timezone.utc).isoformat()
    save_sequence(target)
    print(f"Stopped inbound sequence: {target.get('company')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", metavar="EMAIL_OR_COMPANY")
    args = ap.parse_args()
    if args.check:
        check()
    elif args.status:
        show_status()
    elif args.stop:
        stop_sequence(args.stop)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
