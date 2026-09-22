#!/usr/bin/env python3
"""inbound-post-draft-slack.py — Atomic, idempotent inbound draft poster for Slack.

Slack-side replacement for inbound-post-draft.py. Posts a Block Kit action card
to #rudy-inbound (channel C0000000000), saves a pending file keyed by the Slack
message ts so the listener's button handlers can look up the draft on APPROVE/
EDIT/LATER/SKIP.

Idempotent on sf_id — if a pending Slack file already exists for this sf_id,
returns ALREADY_POSTED without re-posting.

Usage:
  inbound-post-draft-slack.py \
      --to prospect@example.com \
      --subject "datacenterHawk + Acme Capital" \
      --body-file /tmp/draft.txt \
      --lead-name "Leo Wang" \
      --sf-id 00QUM00000XYMRJ2A5 \
      --company "Acme Capital" \
      --what-they-did "Requested DFW market analytics report" \
      [--sf-url https://dch.lightning.force.com/lightning/r/Lead/.../view] \
      [--zapier-permalink https://...]
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

CRED_FILE = Path.home() / ".openclaw" / "slack-rudy.json"
PENDING_DIR = Path.home() / ".openclaw" / "workspace" / "pending-inbound-slack"
TEMPLATE_DIR = Path.home() / ".openclaw" / "workspace" / "inbound-templates"
WATCHER_STATE = Path.home() / ".openclaw" / "workspace" / "data" / "slack-watcher-state.json"


def ack_zapier(permalink):
    """Self-ack the originating Zapier lead in the watcher state after a confirmed post,
    so the watcher stops re-offering it. Derives the message ts from the permalink
    (…/p1787082311179189 -> 1787082311.179189). Best-effort: never breaks the post."""
    if not permalink:
        return
    try:
        import re
        m = re.search(r"/p(\d{10})(\d{6})", permalink)
        if not m:
            return
        ts = f"{m.group(1)}.{m.group(2)}"
        state = {}
        if WATCHER_STATE.exists():
            try:
                state = json.loads(WATCHER_STATE.read_text())
            except Exception:
                state = {}
        acked = set(state.get("acked", []))
        if ts in acked:
            return
        acked.add(ts)
        state["acked"] = sorted(acked)
        WATCHER_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = WATCHER_STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(WATCHER_STATE)
    except Exception:
        pass


def first_name_of(lead_name):
    """First token of the lead name, or 'there' for blanks/placeholders."""
    if lead_name:
        tok = lead_name.strip().split()
        if tok and tok[0].lower() not in ("", "there"):
            return tok[0]
    return "there"


def load_template(name, lead_name, company):
    """Load a canned reply template (deterministic copy + Calendly link + attachment) and
    fill {first_name}/{company} placeholders. Returns a dict with subject/body/body_html/
    attachments. Used for fixed-trigger replies (e.g. GC sample requests) so the copy never
    drifts through the LLM drafter."""
    f = TEMPLATE_DIR / f"{name}.json"
    if not f.exists():
        raise FileNotFoundError(f"template not found: {f}")
    t = json.loads(f.read_text())
    fn = first_name_of(lead_name)
    fill = lambda s: (s or "").replace("{first_name}", fn).replace("{company}", company or "")
    followups = [
        {"n": fu.get("n"), "send_day": fu.get("send_day"), "body": fill(fu.get("body", ""))}
        for fu in t.get("followups", [])
    ]
    return {
        "subject": fill(t.get("subject", "")),
        "body": fill(t.get("body_plain", "")),
        "body_html": fill(t.get("body_html", "")),
        "attachments": t.get("attachments", []),
        "followups": followups,
    }


def load_creds():
    with open(CRED_FILE) as f:
        return json.load(f)


def already_posted(sf_id):
    if not PENDING_DIR.exists():
        return None
    for f in PENDING_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        if data.get("sf_id") == sf_id and data.get("status") == "pending":
            return f
    return None


# Territory routing (added 2026-07-14): APAC contacts -> Kathryn Wong, EMEA -> David
# Sandars, everyone else stays with Hank. The forward buttons never send on their own
# — Hank taps one to approve the internal hand-off email (TO rep, CC Jenn + Steve).
REGION_EMOJI = {"APAC": "🌏", "EMEA": "🌍", "NA": "🗽", "UNKNOWN": "❓"}
ROUTE_REP = {"kathryn": "Kathryn Wong", "david": "David Sandars", "hank": "Hank"}


def build_region_banner(region_info):
    """A section describing where Rudy thinks the contact is based + the suggested route."""
    region = (region_info.get("region") or "UNKNOWN").upper()
    route = region_info.get("route") or "hank"
    loc = region_info.get("location") or region_info.get("country") or ""
    conf = region_info.get("confidence") or "low"
    reason = region_info.get("reasoning") or ""
    linkedin = region_info.get("linkedin_url") or ""
    emoji = REGION_EMOJI.get(region, "❓")

    if route == "hank":
        route_line = "stays with *Hank* (US/Canada or unknown)"
    else:
        route_line = f"suggested route: *{ROUTE_REP.get(route, route)}*"
    loc_str = f" — {loc}" if loc else ""
    text = f"{emoji} *Contact region:* {region}{loc_str}  ·  _{conf} confidence_\n{route_line}"
    if reason:
        text += f"\n_{reason}_"
    if linkedin:
        text += f"\n<{linkedin}|LinkedIn profile>"
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def build_card(lead_name, email, company, what_they_did, subject, body, sf_url,
               zapier_permalink, attachments=None, region_info=None):
    region_info = region_info or {"region": "UNKNOWN", "route": "hank"}
    route = region_info.get("route") or "hank"
    has_reply = bool(body)  # foreign leads (APAC/EMEA) are handed off, not replied to

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🔔 New Inbound Lead — {lead_name}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Name:*\n{lead_name}"},
                {"type": "mrkdwn", "text": f"*Email:*\n{email}"},
                {"type": "mrkdwn", "text": f"*Company:*\n{company or '(none)'}"},
                {"type": "mrkdwn", "text": f"*What they did:*\n{what_they_did or 'No activity found'}"},
            ],
        },
        build_region_banner(region_info),
    ]

    if has_reply:
        body_preview = body if len(body) <= 600 else body[:600] + "..."
        blocks += [
            {"type": "divider"},
            {"type": "section",
             "text": {"type": "mrkdwn", "text": f"*📝 Draft reply*\n*Subject:* {subject}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```{body_preview}```"}},
        ]
        if attachments:
            names = ", ".join(__import__("os").path.basename(a) for a in attachments)
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": f"*📎 Attachment:* {names}"}})

    context_elements = []
    if sf_url:
        context_elements.append({"type": "mrkdwn", "text": f"<{sf_url}|View in Salesforce>"})
    if zapier_permalink:
        context_elements.append({"type": "mrkdwn", "text": f"<{zapier_permalink}|Original Zapier message>"})
    if context_elements:
        blocks.append({"type": "context", "elements": context_elements})

    # --- Action buttons -------------------------------------------------------
    approve_btn = {
        "type": "button", "text": {"type": "plain_text", "text": "✓ Approve & Send"},
        "style": "primary", "action_id": "inbound_approve",
    }
    edit_btn = {
        "type": "button", "text": {"type": "plain_text", "text": "✎ Edit"},
        "action_id": "inbound_edit",
    }
    later_btn = {
        "type": "button", "text": {"type": "plain_text", "text": "⏱ Handle Later"},
        "action_id": "inbound_later",
    }
    skip_btn = {
        "type": "button", "text": {"type": "plain_text", "text": "🗑 Skip"},
        "style": "danger", "action_id": "inbound_skip",
    }
    apac_btn = {
        "type": "button", "text": {"type": "plain_text", "text": "📨 Send to Kathryn (APAC)"},
        "action_id": "inbound_fwd_apac",
    }
    emea_btn = {
        "type": "button", "text": {"type": "plain_text", "text": "📨 Send to David (EMEA)"},
        "action_id": "inbound_fwd_emea",
    }
    # Highlight the researched route only when there's no reply draft competing for
    # the primary slot (Approve & Send owns primary on Hank's own leads).
    if not has_reply:
        if route == "kathryn":
            apac_btn["style"] = "primary"
        elif route == "david":
            emea_btn["style"] = "primary"

    elements = []
    if has_reply:
        elements += [approve_btn, edit_btn]
    elements += [apac_btn, emea_btn]
    if has_reply:
        elements.append(later_btn)
    elements.append(skip_btn)

    blocks.append({"type": "actions", "elements": elements})
    return blocks


def post_card(creds, blocks, fallback_text):
    payload = json.dumps({
        "channel": creds["channels"]["rudy_inbound"],
        "blocks": blocks,
        "text": fallback_text,
    }).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {creds['bot_token']}",
        },
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=30)
    result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"slack chat.postMessage failed: {result}")
    return result["ts"], result["channel"]


def save_pending(ts, channel, payload):
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    final = PENDING_DIR / f"{ts}.json"
    tmp = final.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(final)
    return final


def run_roe(company, domain):
    """Run the single ROE checker (roe-check.py --json). Return parsed dict or None.
    Fail-open (None) on any error so a ROE hiccup never blocks legitimate inbound."""
    import os as _os
    import subprocess as _sp
    try:
        home = _os.path.expanduser("~")
        env = _os.environ.copy()
        env["SF_USE_GENERIC_UNIX_KEYCHAIN"] = "true"
        env["PATH"] = ":".join([
            f"{home}/.local/bin", f"{home}/.nvm/versions/node/v22.22.2/bin",
            "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
        ]) + ":" + env.get("PATH", "")
        cmd = ["python3", f"{home}/.local/bin/roe-check.py", "--json", company or (domain or "")]
        if domain:
            cmd.append(domain)
        res = _sp.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        if res.returncode != 0:
            return None
        i = res.stdout.find("{")
        return json.loads(res.stdout[i:]) if i >= 0 else None
    except Exception:
        return None


def build_roe_block_card(lead_name, email, company, what_they_did, subject, body,
                         sf_url, reason, account_name, owner_name):
    """Warning card for a ROE-blocked inbound lead — NO Approve & Send button, so a
    current customer (or otherwise-protected account) can't be cold-emailed by mistake."""
    body_preview = body if len(body) <= 600 else body[:600] + "..."
    acct = f" *{account_name}*" if account_name else ""
    who = f" · owner/CSM: {owner_name}" if owner_name else ""
    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"⛔ ROE BLOCK — do NOT prospect — {lead_name}"}},
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": (f"*This lead's company is protected by ROE — not a net-new prospect.*\n"
                           f"*Reason:* {reason}\n*Matched account:*{acct}{who}")}},
        {"type": "section",
         "fields": [
             {"type": "mrkdwn", "text": f"*Name:*\n{lead_name}"},
             {"type": "mrkdwn", "text": f"*Email:*\n{email}"},
             {"type": "mrkdwn", "text": f"*Company:*\n{company or '(none)'}"},
             {"type": "mrkdwn", "text": f"*What they did:*\n{what_they_did or 'No activity found'}"},
         ]},
        {"type": "divider"},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*Draft that was NOT sent* (subject: {subject}):"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{body_preview}```"}},
        {"type": "context",
         "elements": [{"type": "mrkdwn",
                       "text": "No *Approve & Send* — route to the account owner/CSM, or send manually only if you're sure."}]},
    ]
    if sf_url:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn", "text": f"<{sf_url}|View in Salesforce>"}]})
    return blocks


def resolve_region(email, name, company):
    """Ask lead-region-classify.py where the contact is based. Best-effort: any failure
    returns an UNKNOWN/hank result so a research hiccup never blocks posting the card."""
    import os as _os
    import subprocess as _sp
    default = {"region": "UNKNOWN", "route": "hank", "location": "", "country": "",
               "confidence": "low", "reasoning": "", "linkedin_url": ""}
    try:
        home = _os.path.expanduser("~")
        cmd = ["python3", f"{home}/.local/bin/lead-region-classify.py",
               "--email", email, "--json"]
        if name:
            cmd += ["--name", name]
        if company:
            cmd += ["--company", company]
        res = _sp.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0 or not res.stdout.strip():
            return default
        out = json.loads(res.stdout.strip())
        return {**default, **out}
    except Exception:
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    ap.add_argument("--subject", help="Subject (optional if --template supplies one)")
    ap.add_argument("--body", help="Email body (or use --body-file)")
    ap.add_argument("--body-file", help="Path to file with body")
    ap.add_argument("--body-html-file", help="Path to file with an HTML body (renders hyperlinks)")
    ap.add_argument("--template", help="Canned reply template name from inbound-templates/ (e.g. gc-sample)")
    ap.add_argument("--attachment", action="append", default=[], help="Local file path to attach (repeatable)")
    ap.add_argument("--lead-name", required=True)
    ap.add_argument("--sf-id", required=True)
    ap.add_argument("--company", default="")
    ap.add_argument("--what-they-did", default="")
    ap.add_argument("--sf-url", default="")
    ap.add_argument("--zapier-permalink", default="")
    # --- Territory routing (2026-07-14). If --region is omitted the script researches
    # it itself via lead-region-classify.py, so routing works even if the caller skips it.
    ap.add_argument("--region", choices=["NA", "APAC", "EMEA", "UNKNOWN"],
                    help="Contact's region. If omitted, the script classifies it.")
    ap.add_argument("--route", choices=["hank", "kathryn", "david"],
                    help="Explicit route override (derived from --region if omitted)")
    ap.add_argument("--region-detail", default="", help="Location string, e.g. 'Singapore'")
    ap.add_argument("--region-confidence", default="", help="high|medium|low")
    ap.add_argument("--region-reasoning", default="", help="One-line rationale")
    ap.add_argument("--linkedin", default="", help="Contact's LinkedIn URL (shown on card + in hand-off)")
    args = ap.parse_args()

    # Cheap idempotency gate FIRST — before the slow region/ROE lookups — so a re-offered
    # lead that was already carded short-circuits instantly (and self-acks the watcher).
    existing = already_posted(args.sf_id)
    if existing:
        try:
            data = json.loads(existing.read_text())
            ts = data.get("slack_message_ts", "?")
        except Exception:
            ts = "?"
        ack_zapier(args.zapier_permalink)
        print(f"ALREADY_POSTED: ts={ts} sf_id={args.sf_id} file={existing.name}")
        return 0

    # Defaults, then a --template fills any it provides, then explicit CLI flags win.
    subject = args.subject
    body = None
    body_html = None
    attachments = list(args.attachment)
    followups = []

    if args.template:
        try:
            tpl = load_template(args.template, args.lead_name, args.company)
        except Exception as e:
            print(f"FAILED: {e}", file=sys.stderr)
            return 2
        subject = subject or tpl["subject"]
        body = tpl["body"] or None
        body_html = tpl["body_html"] or None
        attachments = attachments or tpl["attachments"]
        followups = tpl["followups"]

    if args.body_file:
        body = Path(args.body_file).read_text()
    elif args.body:
        body = args.body
    if args.body_html_file:
        body_html = Path(args.body_html_file).read_text()

    # --- Resolve where the contact is based (research it here if the caller didn't) ---
    ROUTE_BY_REGION = {"APAC": "kathryn", "EMEA": "david", "NA": "hank", "UNKNOWN": "hank"}
    if args.region:
        region_info = {
            "region": args.region,
            "route": args.route or ROUTE_BY_REGION.get(args.region, "hank"),
            "location": args.region_detail, "country": args.region_detail,
            "confidence": args.region_confidence or "medium",
            "reasoning": args.region_reasoning, "linkedin_url": args.linkedin,
        }
    else:
        region_info = resolve_region(args.to, args.lead_name, args.company)
        if args.route:
            region_info["route"] = args.route
        if args.linkedin:
            region_info["linkedin_url"] = args.linkedin
    route = region_info.get("route") or "hank"

    # A foreign lead (APAC/EMEA) is handed off, not replied to — no draft required.
    # Hank's own leads still need the reply subject+body as before.
    if route == "hank":
        if not subject:
            print("FAILED: must provide --subject or a --template with a subject", file=sys.stderr)
            return 2
        if not body:
            print("FAILED: must provide --body, --body-file, or a --template", file=sys.stderr)
            return 2
    else:
        # Drop any drafted reply — we're routing this lead to a regional rep.
        subject, body, body_html = None, None, None

    creds = load_creds()

    # --- ROE gate: never present a one-tap "Approve & Send" for a company we
    # shouldn't cold-prospect (esp. a Current Customer). Reuses roe-check.py so
    # inbound honors the same Rules of Engagement as outbound. Only relevant on the
    # reply path — a foreign lead is an internal hand-off, not cold outreach.
    roe_domain = args.to.split("@", 1)[1].lower() if "@" in args.to else ""
    roe = run_roe(args.company, roe_domain) if route == "hank" else None
    if roe and roe.get("status") == "BLOCKED":
        reason = "; ".join(roe.get("blocking_reasons") or []) or roe.get("status_detail") or "ROE blocked"
        acct0 = (roe.get("accounts") or [{}])[0]
        fallback = f"⛔ ROE BLOCK — {args.lead_name} ({args.to}) — {reason}"
        blocks = build_roe_block_card(
            args.lead_name, args.to, args.company, args.what_they_did, subject, body,
            args.sf_url, reason, acct0.get("name", ""), acct0.get("owner_name", ""))
        try:
            ts, channel = post_card(creds, blocks, fallback)
        except Exception as e:
            print(f"FAILED: post_card error: {e}", file=sys.stderr)
            return 1
        try:
            save_pending(ts, channel, {
                "slack_message_ts": ts, "slack_channel": channel, "to": args.to,
                "subject": subject, "body": body, "lead_name": args.lead_name,
                "company": args.company, "sf_id": args.sf_id, "sf_url": args.sf_url,
                "status": "roe_blocked", "roe_reason": reason,
            })
        except Exception as e:
            print(f"WARN: posted ROE card ts={ts} but save_pending failed: {e}", file=sys.stderr)
        ack_zapier(args.zapier_permalink)
        print(f"ROE_BLOCKED: {args.company} ({roe_domain}) — {reason}; posted warning card ts={ts}")
        return 0
    # not ROE-blocked → normal draft card below

    fallback = f"New Inbound Lead — {args.lead_name} ({args.to})"
    blocks = build_card(
        args.lead_name, args.to, args.company, args.what_they_did,
        subject, body, args.sf_url, args.zapier_permalink, attachments,
        region_info=region_info,
    )

    try:
        ts, channel = post_card(creds, blocks, fallback)
    except Exception as e:
        print(f"FAILED: post_card error: {e}", file=sys.stderr)
        return 1

    payload = {
        "slack_message_ts": ts,
        "slack_channel": channel,
        "to": args.to,
        "subject": subject,
        "body": body,
        "body_html": body_html,
        "attachments": attachments,
        "followups": followups,
        "lead_name": args.lead_name,
        "company": args.company,
        "what_they_did": args.what_they_did,
        "sf_id": args.sf_id,
        "sf_url": args.sf_url,
        "zapier_permalink": args.zapier_permalink,
        # Territory routing — the forward action reads these to build the hand-off email.
        "region": region_info.get("region", "UNKNOWN"),
        "route": route,
        "region_detail": region_info.get("location", ""),
        "region_confidence": region_info.get("confidence", ""),
        "region_reasoning": region_info.get("reasoning", ""),
        "linkedin": region_info.get("linkedin_url", ""),
        "status": "pending",
    }
    try:
        path = save_pending(ts, channel, payload)
    except Exception as e:
        print(f"FAILED: posted ts={ts} but save_pending failed: {e}", file=sys.stderr)
        return 1

    ack_zapier(args.zapier_permalink)
    print(f"POSTED: ts={ts} sf_id={args.sf_id} file={path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
