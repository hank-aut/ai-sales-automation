#!/usr/bin/env python3
"""rudy-slack-listener.py — Socket Mode listener for Rudy.

Handles button clicks + modal submissions for:
- Inbound action cards in #rudy-inbound (Phase 2)
- Outbound action cards in #rudy-outbound (Phase 3)
- DM messages to Rudy (Phase 3B — conversational)

Action IDs:
  inbound_approve / inbound_edit / inbound_later / inbound_skip
  outbound_approve / outbound_edit / outbound_edit_opus / outbound_skip

DM messages route to rudy-chat-slack.py (full Opus + bash tool loop).
"""
import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

CRED_FILE = Path.home() / ".openclaw" / "slack-rudy.json"
LOG_FILE = Path.home() / ".openclaw" / "logs" / "rudy-slack-listener.log"

INBOUND_PENDING = Path.home() / ".openclaw" / "workspace" / "pending-inbound-slack"
INBOUND_ACTION = Path.home() / ".local" / "bin" / "inbound-action-slack.py"
OUTBOUND_PENDING = Path.home() / ".openclaw" / "workspace" / "pending-outbound-slack"
OUTBOUND_ACTION = Path.home() / ".local" / "bin" / "outbound-action-slack.py"
LEARN_ACTION = Path.home() / ".local" / "bin" / "self-improve-action.py"
LEADCONV_ACTION = Path.home() / ".local" / "bin" / "lead-convert-action.py"
CHAT_SCRIPT = Path.home() / ".local" / "bin" / "rudy-chat-slack.py"

# Only these Slack user IDs may drive Rudy's agentic DM handler (full shell access).
# Anyone else DMing Rudy is ignored. Hank Autrey = U083XKNPW0Y.
ALLOWED_DM_USERS = {"U083XKNPW0Y"}
# Same allowlist gates interactive card buttons / modal submits.
ALLOWED_ACTION_USERS = set(ALLOWED_DM_USERS)
CHAT_TMP_DIR = Path("/tmp/rudy-slack-dm-images")

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("rudy-slack")

with open(CRED_FILE) as f:
    cred = json.load(f)

app = App(token=cred["bot_token"])


def reply(client, channel, thread_ts, text):
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


def load_pending(pending_dir, ts):
    f = pending_dir / f"{ts}.json"
    if not f.exists():
        return None
    return json.loads(f.read_text())


def run_action(action_script, action, ts, **kwargs):
    cmd = ["/usr/bin/python3", str(action_script), "--action", action, "--ts", ts]
    for k, v in kwargs.items():
        cmd.extend([f"--{k.replace('_', '-')}", v])
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    out = res.stdout.strip()
    try:
        return json.loads(out.splitlines()[-1]) if out else {"status": "error", "error": "no output"}
    except json.JSONDecodeError:
        return {"status": "error", "error": f"unparseable: {out[:200]} stderr={res.stderr[:200]}"}


# ============== INBOUND HANDLERS ==============

@app.action("inbound_approve")
def handle_inbound_approve(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    ts = body["message"]["ts"]; channel = body["channel"]["id"]; user = body["user"]["id"]
    log.info("inbound_approve clicked by %s ts=%s", user, ts)
    result = run_action(INBOUND_ACTION, "approve", ts)
    if result["status"] == "ok":
        sf_note = " (SF Task logged)" if result.get("sf_task") else " (SF Task skipped — no sf_id)"
        reply(client, channel, ts, f"✅ Sent to *{result.get('lead_name','?')}* at `{result.get('to','?')}`{sf_note}.")
    else:
        reply(client, channel, ts, f"⚠️ <@{user}> Approve FAILED: {result.get('error','unknown')}")


@app.action("inbound_skip")
def handle_inbound_skip(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    ts = body["message"]["ts"]; channel = body["channel"]["id"]; user = body["user"]["id"]
    result = run_action(INBOUND_ACTION, "skip", ts)
    if result["status"] == "ok":
        reply(client, channel, ts, f"🗑 Skipped — draft for *{result.get('lead_name','?')}* discarded.")
    else:
        reply(client, channel, ts, f"⚠️ Skip FAILED: {result.get('error','unknown')}")


@app.action("inbound_later")
def handle_inbound_later(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    ts = body["message"]["ts"]; channel = body["channel"]["id"]; user = body["user"]["id"]
    result = run_action(INBOUND_ACTION, "later", ts)
    if result["status"] == "ok":
        sf_note = " SF task created." if result.get("sf_task") else " (No SF task — no sf_id)"
        reply(client, channel, ts, f"⏱ Deferred — *{result.get('lead_name','?')}* will need a reply.{sf_note}")
    else:
        reply(client, channel, ts, f"⚠️ Handle Later FAILED: {result.get('error','unknown')}")


@app.action("inbound_edit")
def handle_inbound_edit(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    ts = body["message"]["ts"]; channel = body["channel"]["id"]; user = body["user"]["id"]
    pending = load_pending(INBOUND_PENDING, ts)
    if pending is None:
        client.chat_postMessage(channel=channel, thread_ts=ts,
                                text=f"⚠️ <@{user}> Edit FAILED: no pending draft for ts={ts}")
        return
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "inbound_edit_modal",
            "title": {"type": "plain_text", "text": "Edit Reply"},
            "submit": {"type": "plain_text", "text": "Save & Send"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": json.dumps({"channel": channel, "ts": ts}),
            "blocks": [
                {"type": "context", "elements": [{"type": "mrkdwn",
                    "text": f"To: *{pending.get('to','?')}* — {pending.get('lead_name','')}"}]},
                {"type": "input", "block_id": "subject_block",
                 "label": {"type": "plain_text", "text": "Subject"},
                 "element": {"type": "plain_text_input", "action_id": "subject_input",
                             "initial_value": pending.get("subject", "")}},
                {"type": "input", "block_id": "body_block",
                 "label": {"type": "plain_text", "text": "Body"},
                 "element": {"type": "plain_text_input", "action_id": "body_input",
                             "multiline": True, "initial_value": pending.get("body", "")}},
            ],
        },
    )


@app.view("inbound_edit_modal")
def handle_inbound_edit_modal(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    meta = json.loads(body["view"]["private_metadata"])
    channel = meta["channel"]; ts = meta["ts"]; user = body["user"]["id"]
    values = body["view"]["state"]["values"]
    new_subject = values["subject_block"]["subject_input"]["value"]
    new_body = values["body_block"]["body_input"]["value"]
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(new_body); tf.flush(); body_path = tf.name
    try:
        result = run_action(INBOUND_ACTION, "edit", ts, new_subject=new_subject, new_body_file=body_path)
    finally:
        try: os.unlink(body_path)
        except OSError: pass
    if result["status"] == "ok":
        snippet = new_body[:200] + ("..." if len(new_body) > 200 else "")
        sf_note = " (SF Task logged)" if result.get("sf_task") else " (SF Task skipped — no sf_id)"
        reply(client, channel, ts,
              f"✏️ Edited & sent to *{result.get('lead_name','?')}* at `{result.get('to','?')}`.{sf_note}\n*Subject:* {new_subject}\n*Body:* {snippet}")
    else:
        reply(client, channel, ts, f"⚠️ <@{user}> Edit-and-send FAILED: {result.get('error','unknown')}")


def _handle_inbound_forward(region, ack, body, client):
    """Shared handler for the two territory-routing buttons. Hands the lead to the
    regional rep (CC Jenn + Steve). The tap is the approval — nothing auto-sends."""
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    ts = body["message"]["ts"]; channel = body["channel"]["id"]; user = body["user"]["id"]
    log.info("inbound_fwd_%s clicked by %s ts=%s", region, user, ts)
    result = run_action(INBOUND_ACTION, "forward", ts, region=region)
    if result["status"] == "ok":
        cc = ", ".join(result.get("cc", []))
        sf_note = " (SF logged)" if result.get("sf_task") else ""
        reply(client, channel, ts,
              f"📨 Routed *{result.get('lead_name','?')}* to *{result.get('rep','?')}* "
              f"({result.get('region','?')}) at `{result.get('to','?')}` — CC {cc}.{sf_note}")
    else:
        reply(client, channel, ts,
              f"⚠️ <@{user}> Forward FAILED: {result.get('error','unknown')}")


@app.action("inbound_fwd_apac")
def handle_inbound_fwd_apac(ack, body, client):
    _handle_inbound_forward("apac", ack, body, client)


@app.action("inbound_fwd_emea")
def handle_inbound_fwd_emea(ack, body, client):
    _handle_inbound_forward("emea", ack, body, client)


# ============== OUTBOUND HANDLERS ==============

@app.action("outbound_approve")
def handle_outbound_approve(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    ts = body["message"]["ts"]; channel = body["channel"]["id"]; user = body["user"]["id"]
    log.info("outbound_approve clicked by %s ts=%s", user, ts)
    result = run_action(OUTBOUND_ACTION, "approve", ts)
    if result["status"] == "ok":
        e2 = result.get("email_2_day"); e3 = result.get("email_3_day")
        seq_note = f" Sequence active: Email 2 in +{e2}d, Email 3 in +{e3}d." if e2 and e3 else ""
        sf_note = " SF Account/Contact/Task logged." if result.get("account_id") else " (No SF writes — existing account or sf_id absent.)"
        reply(client, channel, ts,
              f"✅ Sent to *{result.get('company','?')}* (`{result.get('to','?')}`).{sf_note}{seq_note}")
    else:
        reply(client, channel, ts, f"⚠️ <@{user}> Approve FAILED: {result.get('error','unknown')}")


@app.action("outbound_skip")
def handle_outbound_skip(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    ts = body["message"]["ts"]; channel = body["channel"]["id"]; user = body["user"]["id"]
    result = run_action(OUTBOUND_ACTION, "skip", ts)
    if result["status"] == "ok":
        reply(client, channel, ts, f"🗑 Skipped — *{result.get('company','?')}* added to cooldown.")
    else:
        reply(client, channel, ts, f"⚠️ Skip FAILED: {result.get('error','unknown')}")


@app.action("outbound_edit")
def handle_outbound_edit(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    ts = body["message"]["ts"]; channel = body["channel"]["id"]; user = body["user"]["id"]
    pending = load_pending(OUTBOUND_PENDING, ts)
    if pending is None:
        client.chat_postMessage(channel=channel, thread_ts=ts,
                                text=f"⚠️ <@{user}> Edit FAILED: no pending file for ts={ts}")
        return
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "outbound_edit_modal",
            "title": {"type": "plain_text", "text": "Edit Outbound"},
            "submit": {"type": "plain_text", "text": "Save & Send"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": json.dumps({"channel": channel, "ts": ts}),
            "blocks": [
                {"type": "context", "elements": [{"type": "mrkdwn",
                    "text": f"To: *{pending.get('contact_email','?')}* — {pending.get('contact_name','')} at {pending.get('company','')}"}]},
                {"type": "input", "block_id": "subject_block",
                 "label": {"type": "plain_text", "text": "Subject"},
                 "element": {"type": "plain_text_input", "action_id": "subject_input",
                             "initial_value": pending.get("subject", "")}},
                {"type": "input", "block_id": "body_block",
                 "label": {"type": "plain_text", "text": "Body"},
                 "element": {"type": "plain_text_input", "action_id": "body_input",
                             "multiline": True, "initial_value": pending.get("body_plain", "")}},
            ],
        },
    )


@app.view("outbound_edit_modal")
def handle_outbound_edit_modal(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    meta = json.loads(body["view"]["private_metadata"])
    channel = meta["channel"]; ts = meta["ts"]; user = body["user"]["id"]
    values = body["view"]["state"]["values"]
    new_subject = values["subject_block"]["subject_input"]["value"]
    new_body = values["body_block"]["body_input"]["value"]
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(new_body); tf.flush(); body_path = tf.name
    try:
        result = run_action(OUTBOUND_ACTION, "edit", ts, new_subject=new_subject, new_body_file=body_path)
    finally:
        try: os.unlink(body_path)
        except OSError: pass
    if result["status"] == "ok":
        snippet = new_body[:200] + ("..." if len(new_body) > 200 else "")
        e2 = result.get("email_2_day"); e3 = result.get("email_3_day")
        seq_note = f" Sequence: Email 2 in +{e2}d, Email 3 in +{e3}d." if e2 and e3 else ""
        reply(client, channel, ts,
              f"✏️ Edited & sent to *{result.get('company','?')}* (`{result.get('to','?')}`).{seq_note}\n*Subject:* {new_subject}\n*Body:* {snippet}")
    else:
        reply(client, channel, ts, f"⚠️ <@{user}> Edit-and-send FAILED: {result.get('error','unknown')}")


@app.action("outbound_edit_opus")
def handle_outbound_edit_opus(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    ts = body["message"]["ts"]; channel = body["channel"]["id"]; user = body["user"]["id"]
    pending = load_pending(OUTBOUND_PENDING, ts)
    if pending is None:
        client.chat_postMessage(channel=channel, thread_ts=ts,
                                text=f"⚠️ <@{user}> Ask Rudy to Edit FAILED: no pending file for ts={ts}")
        return
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "outbound_edit_opus_modal",
            "title": {"type": "plain_text", "text": "Ask Rudy to Edit"},
            "submit": {"type": "plain_text", "text": "Revise"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": json.dumps({"channel": channel, "ts": ts}),
            "blocks": [
                {"type": "context", "elements": [{"type": "mrkdwn",
                    "text": f"Current draft for *{pending.get('company','')}* — Subject: {pending.get('subject','')}"}]},
                {"type": "input", "block_id": "instructions_block",
                 "label": {"type": "plain_text", "text": "What should change?"},
                 "element": {"type": "plain_text_input", "action_id": "instructions_input",
                             "multiline": True,
                             "placeholder": {"type": "plain_text",
                                             "text": "e.g. make it less corporate, mention their recent build, shorter"}}},
            ],
        },
    )


@app.view("outbound_edit_opus_modal")
def handle_outbound_edit_opus_modal(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    meta = json.loads(body["view"]["private_metadata"])
    channel = meta["channel"]; ts = meta["ts"]; user = body["user"]["id"]
    instructions = body["view"]["state"]["values"]["instructions_block"]["instructions_input"]["value"]
    result = run_action(OUTBOUND_ACTION, "edit_opus", ts, instructions=instructions)
    if result["status"] == "ok":
        preview = result.get("revised_preview", "")
        reply(client, channel, ts,
              f"✏️ <@{user}> Rudy revised draft for *{result.get('company','?')}* (edit #{result.get('edit_count','?')}).\n*Instructions:* {instructions}\n*New body preview:* {preview}\n\nCard above updated. Tap Approve when ready.")
    else:
        reply(client, channel, ts, f"⚠️ <@{user}> Opus revise FAILED: {result.get('error','unknown')}")


# ============== DM HANDLER (Phase 3B — Conversational Rudy) ==============

def _download_slack_file(file_obj, bot_token):
    url = file_obj.get("url_private_download") or file_obj.get("url_private")
    if not url:
        return None
    file_id = file_obj.get("id", "unknown")
    name = file_obj.get("name", "attachment")
    ext = name[name.rfind("."):] if "." in name else ""
    local = CHAT_TMP_DIR / f"{file_id}{ext}"
    CHAT_TMP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bot_token}"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(local, "wb") as out:
            out.write(resp.read())
        return str(local)
    except Exception as e:
        log.error("file download failed for %s: %s", file_id, e)
        return None


@app.event("message")
def handle_dm_message(body, event, client):
    """DMs to Rudy — spawn rudy-chat-slack.py to think + reply."""
    channel = event.get("channel", "")
    channel_type = event.get("channel_type", "")
    user = event.get("user", "")
    text = event.get("text", "") or ""
    subtype = event.get("subtype")

    if not (channel.startswith("D") or channel_type == "im"):
        return
    if subtype in ("bot_message", "message_changed", "message_deleted", "channel_join"):
        return
    if event.get("bot_id"):
        return

    if user not in ALLOWED_DM_USERS:
        log.warning("IGNORED DM from non-allowlisted user %s in %s: %s",
                    user, channel, text[:100])
        return

    files = event.get("files") or []
    if not text.strip() and not files:
        return

    log.info("DM from %s in %s: %s%s", user, channel, text[:100],
             (" +" + str(len(files)) + " file(s)") if files else "")

    image_paths = []
    for f in files:
        mt = (f.get("mimetype") or "").lower()
        if mt.startswith("image/"):
            path = _download_slack_file(f, cred["bot_token"])
            if path:
                image_paths.append(path)

    cmd = ["/usr/bin/python3", str(CHAT_SCRIPT),
           "--channel", channel, "--user", user, "--text", text]
    if image_paths:
        cmd.extend(["--image-files", ",".join(image_paths)])

    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception as e:
        log.error("failed to spawn chat handler: %s", e)


# ============== Test handlers (kept for hello-world card) ==============

@app.action("test_approve")
def handle_test_approve(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    reply(client, body["channel"]["id"], body["message"]["ts"],
          f"✅ <@{body['user']['id']}> tapped *Approve* (test card placeholder).")


@app.action("test_skip")
def handle_test_skip(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    reply(client, body["channel"]["id"], body["message"]["ts"],
          f"🗑 <@{body['user']['id']}> tapped *Skip* (test card placeholder).")


@app.action("test_later")
def handle_test_later(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    reply(client, body["channel"]["id"], body["message"]["ts"],
          f"⏱ <@{body['user']['id']}> tapped *Handle Later* (test card placeholder).")


@app.action("test_edit")
def handle_test_edit(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal", "callback_id": "test_edit_modal",
            "title": {"type": "plain_text", "text": "Edit Email (test)"},
            "submit": {"type": "plain_text", "text": "Save & Send"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": json.dumps({"channel": body["channel"]["id"], "thread_ts": body["message"]["ts"]}),
            "blocks": [
                {"type": "input", "block_id": "subject_block",
                 "label": {"type": "plain_text", "text": "Subject"},
                 "element": {"type": "plain_text_input", "action_id": "subject_input",
                             "initial_value": "datacenterHawk + Equinix"}},
                {"type": "input", "block_id": "body_block",
                 "label": {"type": "plain_text", "text": "Body"},
                 "element": {"type": "plain_text_input", "action_id": "body_input",
                             "multiline": True, "initial_value": "Hi John..."}},
            ],
        },
    )


@app.view("test_edit_modal")
def handle_test_edit_modal(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    meta = json.loads(body["view"]["private_metadata"])
    values = body["view"]["state"]["values"]
    subject = values["subject_block"]["subject_input"]["value"]
    body_text = values["body_block"]["body_input"]["value"]
    snippet = body_text[:200] + ("..." if len(body_text) > 200 else "")
    client.chat_postMessage(
        channel=meta["channel"], thread_ts=meta["thread_ts"],
        text=f"✏️ <@{body['user']['id']}> edited (test card):\n*Subject:* {subject}\n*Body:* {snippet}",
    )


# ============== SELF-IMPROVE (LEARN) HANDLERS ==============

@app.action("learn_select")
def handle_learn_select(ack, body, client):
    # Checkbox toggles only need an ack so Slack doesn't show an error; state is read
    # when the user clicks Apply.
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return


def _learn_checked_idxs(body):
    """Collect checked rule indices from all learn_cb_* blocks' state."""
    idxs = []
    state = (body.get("state") or {}).get("values") or {}
    for block_id, actions in state.items():
        if not block_id.startswith("learn_cb_"):
            continue
        sel = (actions.get("learn_select") or {}).get("selected_options") or []
        for opt in sel:
            v = opt.get("value")
            if v is not None:
                idxs.append(str(v))
    return sorted(set(idxs), key=lambda x: int(x) if x.isdigit() else 0)


@app.action("learn_apply")
def handle_learn_apply(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    ts = body["message"]["ts"]; channel = body["channel"]["id"]; user = body["user"]["id"]
    idxs = _learn_checked_idxs(body)
    log.info("learn_apply by %s ts=%s checked=%s", user, ts, idxs)
    if not idxs:
        reply(client, channel, ts, f"⚠️ <@{user}> nothing checked — tick the rules to apply first.")
        return
    result = run_action(LEARN_ACTION, "apply", ts, rules=",".join(idxs))
    if result.get("status") == "ok":
        rules = result.get("rules", [])
        listed = "\n".join(f"• {r}" for r in rules)
        tgts = ", ".join(result.get("targets", []))
        skills = ", ".join(result.get("skills_updated", []))
        bans = result.get("banned_phrases_added", [])
        extra = ""
        if skills:
            extra += f"\n_live now in:_ {skills} + outbound prompt"
        if bans:
            extra += f"\n_hard-banned phrases:_ {', '.join(bans)}"
        if result.get("rejected"):
            extra += f"\n⚠️ {result['rejected']} rejected by safety denylist"
        reply(client, channel, ts,
              f"✅ <@{user}> applied *{result.get('applied', result.get('approved',0))}* rule(s) "
              f"(targets: {tgts}):\n{listed}{extra}")
    else:
        reply(client, channel, ts, f"⚠️ Apply FAILED: {result.get('error','unknown')}")


@app.action("learn_skip")
def handle_learn_skip(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    ts = body["message"]["ts"]; channel = body["channel"]["id"]; user = body["user"]["id"]
    result = run_action(LEARN_ACTION, "skip", ts)
    if result.get("status") == "ok":
        reply(client, channel, ts, f"🗑 Skipped all proposals from this card.")
    else:
        reply(client, channel, ts, f"⚠️ Skip FAILED: {result.get('error','unknown')}")



# ============== LEAD-CONVERSION HANDLERS ==============

@app.action("leadconv_approve")
def handle_leadconv_approve(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    ts = body["message"]["ts"]; channel = body["channel"]["id"]; user = body["user"]["id"]
    log.info("leadconv_approve clicked by %s ts=%s", user, ts)
    result = run_action(LEADCONV_ACTION, "approve", ts)
    if result.get("status") == "ok":
        reply(client, channel, ts,
              f"\u2705 <@{user}> approved \u2014 converting *{result.get('count','?')}* leads now. "
              f"This runs in the background; I'll post the result here when done.")
    else:
        reply(client, channel, ts, f"\u26a0\ufe0f <@{user}> Approve FAILED: {result.get('error','unknown')}")


@app.action("leadconv_skip")
def handle_leadconv_skip(ack, body, client):
    ack()
    if body.get("user", {}).get("id") not in ALLOWED_ACTION_USERS:
        log.warning("IGNORED action from non-allowlisted user %s",
                    body.get("user", {}).get("id"))
        return
    ts = body["message"]["ts"]; channel = body["channel"]["id"]; user = body["user"]["id"]
    result = run_action(LEADCONV_ACTION, "skip", ts)
    if result.get("status") == "ok":
        reply(client, channel, ts, f"\U0001f5d1 Skipped \u2014 {result.get('skipped','?')} leads left unconverted. Nothing changed in Salesforce.")
    else:
        reply(client, channel, ts, f"\u26a0\ufe0f Skip FAILED: {result.get('error','unknown')}")


if __name__ == "__main__":
    handler = SocketModeHandler(app, cred["app_token"])
    log.info("Starting Rudy Slack Socket Mode listener (with DM handler)...")
    handler.start()
