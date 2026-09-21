#!/usr/bin/env python3
"""outbound-post-card-slack.py — Atomic, idempotent outbound prospect poster for Slack.

Drop-in replacement for outbound-post-card.py. Same CLI (one positional arg: company name),
same input (~/.openclaw/workspace/outbound-v2/pipeline-results.json), but posts a Block Kit
action card to #rudy-outbound and saves a Slack pending file.

Buttons on the card:
  ✓ Approve & Send       (action_id: outbound_approve)
  ✎ Edit (direct)        (action_id: outbound_edit)
  ✏️ Ask Rudy to Edit    (action_id: outbound_edit_opus)
  🗑 Skip                 (action_id: outbound_skip)

The pending file schema mirrors what telegram-responder.do_approve consumes — same field
names (sf_account_id, sf_type, sf_subtype, body_html, body_plain, roe_status, domain, etc.)
so outbound-action-slack.py can reuse the existing approval/SF-create/sequence logic.
"""
import argparse
import html
import json
import re
import ssl
import sys
import urllib.request
from pathlib import Path

CRED_FILE = Path.home() / ".openclaw" / "slack-rudy.json"
PENDING_DIR = Path.home() / ".openclaw" / "workspace" / "pending-outbound-slack"
STATE_FILE = Path.home() / ".openclaw" / "workspace" / "outbound-v2" / "pipeline-results.json"
SENT_STATE = Path.home() / ".openclaw" / "workspace" / "outbound-v2" / "state.json"

# Same Type→Subtype map as outbound-post-card.py (SF dependent picklist)
SF_TYPE_SUBTYPE_MAP = {
    "Investor":   ["Hedge Fund", "Institutional", "PE/RE Managers", "Sell-Side/Advisory", "Strategic / Infra Energy"],
    "Provider":   ["Retail", "Wholesale", "Hyperscale", "Multi"],
    "User":       ["Cloud", "Social Media", "Chip Maker", "Model Builder", "Neocloud"],
    "Vendor":     ["Utilities & Hold Cos", "Retail Energy Providers", "IPPs and Renewable Developers",
                   "Grid-Enablement & Infra/Tech", "Construction", "Telco", "Equipment", "Service", "Insurance"],
    "Consultant": ["Full Service", "Government", "Real Estate", "Research Aggregator", "Tax/Valuation"],
}

SIG_HTML = (
    '<br><br>'
    '<span style="font-family:Aptos,sans-serif;font-size:11pt;">'
    '<a href="mailto:you@example.com" style="color:#0078D4;font-weight:bold;text-decoration:none;">Hank Autrey</a><br>'
    '<b>Sr. Business Development, datacenterHawk</b><br>'
    '555-555-5555 | datacenterhawk.com<br>'
    '14785 Preston Rd., Suite 470 | Dallas, TX 75254'
    '</span>'
)


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


def classify_sf_type_subtype(company, persona, signal, contact_title, body_preview=""):
    type_options = "\n".join(f"  - {t}: {', '.join(subs)}" for t, subs in SF_TYPE_SUBTYPE_MAP.items())
    persona_to_default = {
        "investor":   ("Investor",   "Institutional"),
        "provider":   ("Provider",   "Multi"),
        "user":       ("User",       "Cloud"),
        "vendor":     ("Vendor",     "Equipment"),
        "consultant": ("Consultant", "Full Service"),
    }
    fallback = persona_to_default.get((persona or "").lower(), ("Vendor", "Equipment"))
    key = _load_anthropic_key()
    if not key:
        return fallback

    prompt = (
        "Classify this data center industry company into the right SF Account Type + Subtype.\n\n"
        f"Company: {company}\nPipeline persona hint: {persona}\nSignal/news: {signal or '(none)'}\n"
        f"Contact title: {contact_title or '(none)'}\nEmail body preview: {body_preview[:500]}\n\n"
        "Valid Type+Subtype combinations (pick exactly one Type and one Subtype that goes WITH it):\n"
        f"{type_options}\n\n"
        "Type definitions:\n"
        "- Investor: financial firms (PE, infra funds, REITs, hedge funds, sell-side analysts).\n"
        "- Provider: own/operate data center facilities (colo, hyperscale DC operators).\n"
        "- User: USE data center capacity (hyperscalers, AI/ML, social media, chip makers, neoclouds).\n"
        "- Vendor: sells equipment/services INTO data centers (power, cooling, fiber, etc.).\n"
        "- Consultant: advisory firms (real estate, tax, research aggregators).\n\n"
        'Output ONLY: {"type": "...", "subtype": "..."} — JSON, no commentary.'
    )

    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({"model": "claude-opus-4-8", "max_tokens": 200,
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            method="POST",
        )
        ctx = ssl.create_default_context()
        resp = urllib.request.urlopen(req, context=ctx, timeout=45)
        result = json.loads(resp.read())
        text = (result.get("content") or [{}])[0].get("text", "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return fallback
        parsed = json.loads(m.group())
        t = parsed.get("type", "").strip()
        s = parsed.get("subtype", "").strip()
        if t in SF_TYPE_SUBTYPE_MAP and s in SF_TYPE_SUBTYPE_MAP[t]:
            return (t, s)
        return fallback
    except Exception:
        return fallback


def already_sent(company_lc):
    """Return the previous outbound-action-slack.py state entry for this company
    if its status indicates an action has already been taken (sent/skipped/blocked).
    Used to short-circuit duplicate posts after the 30-min cron rediscovers the
    same prospect (pipeline-results.json status stays READY post-send because
    outbound-action-slack.py writes to a separate state.json)."""
    if not SENT_STATE.exists():
        return None
    try:
        s = json.loads(SENT_STATE.read_text())
    except Exception:
        return None
    rec = s.get(company_lc)
    if not rec:
        return None
    if rec.get("status") in ("sent", "skipped", "blocked"):
        return rec
    return None


def already_posted(company_lc):
    if not PENDING_DIR.exists():
        return None
    for f in PENDING_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        if data.get("company", "").lower().strip() == company_lc and data.get("status") == "pending":
            return f
    return None


def build_html_body(plain_body):
    body = plain_body.rstrip()
    for tail in ("\nHank Autrey", "\n\nHank", "\nHank"):
        if body.endswith(tail):
            body = body[: -len(tail)].rstrip()
            break
    return ('<div style="font-family:Aptos,sans-serif;font-size:11pt;">'
            + html.escape(body).replace("\n", "<br>")
            + SIG_HTML
            + '</div>')


def build_card_blocks(company, contact, persona, source, sf_status, fit, subject, body, roe="?", why=None):
    name = contact.get("name") or "?"
    title = contact.get("title") or "?"
    email = contact.get("email") or "?"
    body_preview = body if len(body) <= 1200 else body[:1200] + "..."

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🎯 OUTBOUND — {company}"}},
    ]
    if why:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": why}]})
    blocks += [
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Contact:*\n{name}"},
                {"type": "mrkdwn", "text": f"*Title:*\n{title}"},
                {"type": "mrkdwn", "text": f"*Email:*\n{email}"},
                {"type": "mrkdwn", "text": f"*Fit:*\n{fit}"},
                {"type": "mrkdwn", "text": f"*Persona:*\n{persona}"},
                {"type": "mrkdwn", "text": f"*Source:*\n{source}"},
                {"type": "mrkdwn", "text": f"*SF Account:*\n{sf_status}"},
                {"type": "mrkdwn", "text": f"*ROE:*\n✅ {roe}"},
            ],
        },
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Subject:* {subject}"}},
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
    return blocks


def post_card(creds, blocks, fallback):
    payload = json.dumps({
        "channel": creds["channels"]["rudy_outbound"],
        "blocks": blocks,
        "text": fallback,
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


def save_pending(ts, payload):
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    final = PENDING_DIR / f"{ts}.json"
    tmp = final.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(final)
    return final


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("company", help="Company name (must match pipeline-results.json key)")
    args = ap.parse_args()

    company_arg = args.company
    company_lc = company_arg.lower().strip()

    existing = already_posted(company_lc)
    if existing:
        try:
            data = json.loads(existing.read_text())
            ts = data.get("slack_message_ts", "?")
        except Exception:
            ts = "?"
        print(f"ALREADY_POSTED: company={company_arg} ts={ts} file={existing.name}")
        return 0

    prior = already_sent(company_lc)
    if prior:
        print(
            f"ALREADY_ACTIONED: company={company_arg} status={prior.get('status')!r} "
            f"last_contact={prior.get('last_contact')!r} (state.json) — not reposting"
        )
        return 0

    if not STATE_FILE.exists():
        print(f"FAILED: pipeline-results.json missing at {STATE_FILE}", file=sys.stderr)
        return 1
    state = json.loads(STATE_FILE.read_text())
    p = state.get(company_lc)
    if not p:
        print(f"FAILED: no pipeline-results entry for {company_arg!r}", file=sys.stderr)
        return 1
    if p.get("status") != "READY":
        print(f"FAILED: prospect status is {p.get('status')!r}, not READY", file=sys.stderr)
        return 1

    contact = (p.get("contact") or {}).get("best_contact") or {}
    if not contact.get("email"):
        print(f"FAILED: no contact email for {company_arg!r}", file=sys.stderr)
        return 1

    emails = (p.get("emails") or {}).get("emails") or []
    if not emails:
        print(f"FAILED: no draft emails for {company_arg!r}", file=sys.stderr)
        return 1

    company = p.get("company") or company_arg
    persona = p.get("persona") or "?"
    source = contact.get("source") or (p.get("contact") or {}).get("email_source") or "?"
    roe_status = (p.get("roe") or {}).get("status_detail") or (p.get("roe") or {}).get("status") or "?"
    sf_status = "Existing" if (p.get("roe") or {}).get("accounts") else "New"
    # Fit is company-level (from company-research.py), stored on the prospect record under "research".
    # Old code read contact.get("fit") which never existed — Apollo contact data has no fit field.
    fit_score = (p.get("research") or {}).get("fit_score")
    if isinstance(fit_score, (int, float)):
        fit_reason = (p.get("research") or {}).get("fit_reason") or ""
        fit = f"{int(fit_score)}/10" + (f" — {fit_reason}" if fit_reason else "")
    else:
        fit = "?/10"

    subject = emails[0].get("subject", f"datacenterHawk + {company}")
    plain_body = emails[0].get("body", "")

    # Competitor-outreach leads carry a why-line so Hank knows the card came from a call.
    why = None
    co = p.get("competitor_outreach")
    if co and co.get("prospect_company"):
        call = co.get("transcript_title") or "a recent call"
        ptitle = co.get("prospect_title")
        psrc = co.get("prospect_title_source")
        # Only claim "same-title" when we actually resolved the prospect's title
        # (transcript / apollo / web). If it fell back to generic persona titles
        # (no source), say so honestly instead of implying a title match.
        if ptitle and psrc:
            match_note = f"Same-title contact (their {ptitle}), likely shares their pain."
        else:
            match_note = "Matched on function/persona, not an exact title."
        why = (f"🧭 Prospected as a competitor to *{co['prospect_company']}* "
               f"because of your call with them (\"{call}\"). {match_note}")

    blocks = build_card_blocks(company, contact, persona, source, sf_status, fit, subject, plain_body,
                               roe=roe_status, why=why)
    creds = json.loads(CRED_FILE.read_text())

    try:
        ts, channel = post_card(creds, blocks, f"OUTBOUND PROSPECT — {company}")
    except Exception as e:
        print(f"FAILED: post_card error: {e}", file=sys.stderr)
        return 1

    body_html = build_html_body(plain_body)
    roe_accounts = (p.get("roe") or {}).get("accounts") or []
    sf_account_id = roe_accounts[0].get("id") if roe_accounts else None
    raw_roe_status = (p.get("roe") or {}).get("status")

    sf_type, sf_subtype = classify_sf_type_subtype(
        company=company, persona=persona, signal=p.get("signal", ""),
        contact_title=contact.get("title", ""), body_preview=plain_body,
    )
    print(f"  classified: Type={sf_type}, Subtype={sf_subtype}", file=sys.stderr)

    draft = {
        "slack_message_ts": ts,
        "slack_channel": channel,
        "company": company,
        "contact_email": contact.get("email"),
        "contact_name": contact.get("name", ""),
        "contact_title": contact.get("title", ""),
        "subject": subject,
        "body_html": body_html,
        "body_plain": plain_body,
        "sf_account_id": sf_account_id,
        "roe_status": raw_roe_status,
        "sf_type": sf_type,
        "sf_subtype": sf_subtype,
        "domain": p.get("domain"),
        "sf_region": p.get("sf_region") or "North America",
        "persona": persona,
        "status": "pending",
        "created": p.get("processed_at"),
    }

    try:
        path = save_pending(ts, draft)
    except Exception as e:
        print(f"FAILED: posted ts={ts} but save_pending failed: {e}", file=sys.stderr)
        return 1

    print(f"POSTED: ts={ts} company={company} file={path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
