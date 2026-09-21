#!/usr/bin/env python3
"""
outbound-pipeline.py - End-to-end outbound prospecting pipeline.

Flow: Signal → ROE Check → Contact Finder → Email Drafter → Output

Usage:
  # Single company
  python3 outbound-pipeline.py "Company Name" "domain.com" --persona investor

  # Batch from CSV (columns: company_name, domain, persona)
  python3 outbound-pipeline.py --batch /path/to/prospects.csv

  # Daily signal scan (pulls from RSS + Confluence + SF leads)
  python3 outbound-pipeline.py --daily-scan

  # Re-run CLEAR accounts from previous ROE results
  python3 outbound-pipeline.py --reprocess-clear
"""

import json
import sys
sys.path.insert(0, str(__import__('pathlib').Path.home() / '.local/bin'))
from email_safety import validate_email_before_send
import subprocess
import sys
import os
import re
import ssl
import urllib.request
import urllib.parse
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
APOLLO_BASE = "https://api.apollo.io/api/v1"

# --- Config ---
def _resolve_state_dir():
    """Prefer iCloud for local Mac; fall back to openclaw workspace for remote/cron.
    Must match the resolution used by outbound-daily.py so both scripts read/write
    the same pipeline-results.json on a given machine. See roe-check.py for the
    original pattern + the macOS TCC rationale."""
    candidates = [
        Path.home() / ".openclaw/workspace/outbound-v2",
        Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/hank-vault/outbound-v2",
    ]
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            test = d / ".rw_test"
            test.write_text("ok")
            if test.read_text() != "ok":
                raise OSError("readback mismatch")
            test.unlink()
            existing = d / "pipeline-results.json"
            if existing.exists():
                existing.read_text()
            return d
        except (PermissionError, OSError):
            continue
    return Path("/tmp/outbound-v2")


STATE_DIR = _resolve_state_dir()
VAULT = STATE_DIR.parent
PROSPECTS_FILE = STATE_DIR / "pipeline-results.json"
EXCLUSION_FILE = STATE_DIR / "exclusion-list.json"
SEQUENCE_DIR = STATE_DIR / "sequences"
DAILY_LOG_DIR = STATE_DIR / "daily-logs"

QDRANT_URL = "http://localhost:6333"

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv


def log(msg):
    if VERBOSE:
        print(f"  [pipeline] {msg}", file=sys.stderr)


def ensure_dirs():
    for d in [STATE_DIR, SEQUENCE_DIR, DAILY_LOG_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_exclusions():
    if EXCLUSION_FILE.exists():
        return json.loads(EXCLUSION_FILE.read_text())
    return {"companies": [], "domains": []}


def save_exclusion(company, domain=None, reason="manual"):
    exclusions = load_exclusions()
    exclusions["companies"].append({
        "company": company,
        "domain": domain,
        "reason": reason,
        "added": datetime.now(timezone.utc).isoformat(),
    })
    if domain:
        exclusions["domains"].append(domain)
    EXCLUSION_FILE.write_text(json.dumps(exclusions, indent=2))


def is_excluded(company, domain=None):
    exclusions = load_exclusions()
    company_lower = company.lower().strip()
    for exc in exclusions.get("companies", []):
        if exc.get("company", "").lower().strip() == company_lower:
            return True
    if domain and domain in exclusions.get("domains", []):
        return True
    return False


def load_current_customers():
    """Load current customer names for filtering."""
    customers_file = VAULT / "current-customers/Current Customers-2026-03-09-09-18-41.xlsx"
    if not customers_file.exists():
        return set()
    try:
        import openpyxl
        wb = openpyxl.load_workbook(customers_file)
        ws = wb.active
        names = set()
        for row in ws.iter_rows(min_row=2, values_only=True):
            vals = [str(v or "").strip() for v in row]
            if "Current Customer" in vals and len(vals) > 3 and vals[3]:
                names.add(vals[3].lower().strip())
        return names
    except Exception:
        return set()


# --- Apollo org search (bootstrap domain when signal source didn't include one) ---
def apollo_org_search(company_name):
    """Look up an organization's primary domain by name. Returns domain string or None.
    Used when prospect arrives without a domain so company-research.py (Layer 2 fit) can run."""
    if not company_name:
        return None
    payload = {"q_organization_name": company_name, "page": 1, "per_page": 5}
    req = urllib.request.Request(
        f"{APOLLO_BASE}/organizations/search",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": APOLLO_API_KEY,
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
            result = json.loads(resp.read())
        orgs = result.get("organizations", [])
        if not orgs:
            return None
        target = company_name.strip().lower()
        # Prefer exact name match, then starts-with, then any close match
        for o in orgs:
            n = (o.get("name") or "").strip().lower()
            if n == target:
                return o.get("primary_domain")
        for o in orgs:
            n = (o.get("name") or "").strip().lower()
            if n.startswith(target) or target.startswith(n):
                return o.get("primary_domain")
        first = orgs[0]
        first_name_lc = (first.get("name") or "").strip().lower()
        if target in first_name_lc or first_name_lc in target:
            return first.get("primary_domain")
        return None
    except Exception as e:
        log(f"Apollo org search error: {e}")
        return None


# --- ROE Check (calls external script) ---
def run_roe_check(company, domain=None):
    cmd = ["python3", str(Path.home() / ".local/bin/roe-check.py"), company]
    if domain:
        cmd.append(domain)
    cmd.append("--json")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    try:
        return json.loads(result.stdout)
    except Exception:
        return {"status": "ERROR", "company": company, "error": result.stderr[:500]}


# --- Ensure SF Contact Exists (required for EAC) ---
def ensure_sf_contact(account_id, contact_info, company_name):
    """Check if contact exists in SF. If not, create it so EAC can sync emails."""
    email = contact_info.get("email", "")
    if not email:
        return

    # Check if contact already exists
    email_clean = email.replace("'", "\\'")
    try:
        existing = subprocess.run(
            ["sf", "data", "query", "--query",
             f"SELECT Id, Name FROM Contact WHERE Email = '{email_clean}' LIMIT 1",
             "--target-org", "you@example.com", "--json"],
            capture_output=True, text=True, timeout=15,
        )
        records = json.loads(existing.stdout).get("result", {}).get("records", [])
        if records:
            log(f"SF Contact already exists: {records[0].get('Name')} ({records[0].get('Id')})")
            return
    except Exception as e:
        log(f"SF Contact lookup failed ({e}), skipping creation to avoid duplicates")
        return

    # Create the contact
    first_name = contact_info.get("first_name", "")
    last_name = contact_info.get("last_name", "")
    if not first_name and not last_name:
        name_parts = contact_info.get("name", "").split(None, 1)
        first_name = name_parts[0] if name_parts else ""
        last_name = name_parts[1] if len(name_parts) > 1 else first_name
        if first_name == last_name:
            first_name = ""

    title = contact_info.get("title", "")

    create_cmd = (
        f"sf data create record --sobject Contact --values \""
        f"AccountId='{account_id}' "
        f"FirstName='{first_name.replace(chr(39), '')}' "
        f"LastName='{last_name.replace(chr(39), '')}' "
        f"Email='{email}' "
        f"Title='{title.replace(chr(39), '')}'\" --json"
    )

    result = subprocess.run(
        ["sf", "data", "create", "record", "--sobject", "Contact",
         "--values",
         f"AccountId='{account_id}' FirstName='{first_name}' LastName='{last_name}' Email='{email}' Title='{title}'",
         "--json", "-o", "you@example.com"],
        capture_output=True, text=True, timeout=15,
    )
    try:
        data = json.loads(result.stdout)
        new_id = data.get("result", {}).get("id", "")
        if new_id:
            print(f"📇  Created SF Contact: {first_name} {last_name} ({new_id})")
        else:
            log(f"SF Contact creation response: {result.stdout[:200]}")
    except Exception as e:
        log(f"SF Contact creation error: {e}")


# --- Contact Finder (calls external script) ---
def run_contact_finder(company, domain=None, persona=None, sf_account_id=None):
    cmd = ["python3", str(Path.home() / ".local/bin/contact-finder.py"), company]
    if domain:
        cmd.append(domain)
    cmd.extend(["--json"])
    if persona:
        cmd.extend(["--persona", persona])
    if sf_account_id:
        cmd.extend(["--sf-account-id", sf_account_id])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    try:
        return json.loads(result.stdout)
    except Exception:
        return {"company": company, "error": result.stderr[:500]}


# --- Email Drafter ---
def _load_voyage_key():
    """Read Voyage API key. Try openclaw path first (works under launchd) then iCloud."""
    import re as _re
    for p in [Path.home() / ".openclaw/config/api-keys.md",
              Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/hank-vault/config/api-keys.md"]:
        try:
            if p.exists():
                m = _re.search(r"## Voyage AI[\s\S]*?API Key[^`]*`([^`]+)`", p.read_text())
                if m: return m.group(1).strip()
        except (PermissionError, OSError): continue
        except Exception: pass
    return os.environ.get("VOYAGE_API_KEY") or None


def search_email_vectors(query_text, limit=5):
    """Search Qdrant hank_sent_emails_v2 (voyage-3, 1024 dim) for similar Hank emails."""
    key = _load_voyage_key()
    if not key:
        log("No Voyage API key - skipping email vector search")
        return []

    try:
        # Embed query via Voyage voyage-3
        req = urllib.request.Request(
            "https://api.voyageai.com/v1/embeddings",
            data=json.dumps({"input":[query_text[:8000]],"model":"voyage-3","input_type":"query"}).encode(),
            headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},
        )
        emb_resp = json.loads(urllib.request.urlopen(req, context=SSL_CTX, timeout=20).read())
        vector = emb_resp["data"][0]["embedding"]

        # Search Qdrant v2 collection
        data = json.dumps({"vector": vector, "limit": limit, "with_payload": True}).encode()
        req = urllib.request.Request(
            f"{QDRANT_URL}/collections/hank_sent_emails_v2/points/search",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        result = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return [
            {
                "score": r["score"],
                "text": (r["payload"].get("text") or r["payload"].get("reply_body") or "")[:600],
                "subject": r["payload"].get("subject", ""),
            }
            for r in result.get("result", [])
        ]
    except Exception as e:
        log(f"Email vector search error: {e}")
        return []


# search_confluence_vectors removed 2026-04-21 - was OpenAI-based, results were never used in
# drafts (intel_section was always empty per Confluence privacy rules), and it depended on a
# retired host for the OpenAI key. Dead code.


def load_tone_guide():
    """Load full tone guide - this is critical for draft quality."""
    tone_file = VAULT / "memory/TONE.md"
    if tone_file.exists():
        return tone_file.read_text()
    return ""


def load_local_email_examples(persona=None):
    """Fallback: load actual sent email files from vault if Qdrant is unavailable."""
    sent_dir = VAULT / "emails-sent"
    if not sent_dir.exists():
        return []

    # Search for outbound/cold email patterns in sent emails
    examples = []
    keywords = ["datacenterHawk", "walk you through", "market intelligence", "platform", "open to"]
    for f in sorted(sent_dir.glob("*.md"), reverse=True)[:200]:
        try:
            content = f.read_text()
            if any(kw.lower() in content.lower() for kw in keywords):
                # Extract just the body (skip frontmatter)
                body = content.split("---", 2)[-1].strip() if "---" in content else content
                if 100 < len(body) < 1500:  # Right length for cold outreach
                    examples.append(body[:600])
                    if len(examples) >= 8:
                        break
        except Exception:
            continue
    return examples


# Hank's standard capability line (kept roughly verbatim in Email 1).
HANK_CAPABILITY_LINE = (
    "We track supply, vacancy, absorption, and pricing across roughly 400 markets, "
    "plus company portfolios and infrastructure layers."
)

# Generic corporate suffix words stripped to make a company name INFORMAL for display
# (subject lines + email body). "Holder Construction" -> "Holder", "Hank Digital
# Infrastructure" -> "Hank Digital". DISPLAY ONLY — always keep the full legal name for
# SF/ROE/dedup. Only TRAILING words are stripped, so leading words are safe
# ("Global Switch" stays "Global Switch"). See feedback_company_informal_display /
# feedback_outbound_subject_strip_suffixes.
_INFORMAL_SUFFIXES = {
    "constructors", "constructor", "construction", "builders", "builder", "group",
    "holdings", "holding", "inc", "llc", "ltd", "corp", "corporation", "company", "co",
    "partners", "partner", "capital", "realty", "properties", "property", "estate",
    "development", "developers", "developer", "infrastructure", "technologies",
    "technology", "solutions", "solution", "systems", "industries", "enterprises",
    "international", "global", "services", "ventures", "management", "advisors", "advisers",
    "investment", "investments",
}


def informal_company(name):
    """Informal DISPLAY name for subjects + email body. Drops a parenthetical
    expansion ('RLB (Rider Levett Bucknall)' -> 'RLB') and strips trailing generic
    corporate suffix words ('Holder Construction' -> 'Holder'). Keep the FULL legal
    name everywhere else (SF/ROE/dedup) — this is display only."""
    if not name:
        return name
    # Drop a parenthetical: "RLB (Rider Levett Bucknall)" -> "RLB",
    # "Faithful+Gould (Atkins)" -> "Faithful+Gould".
    if "(" in name and name.split("(", 1)[0].strip():
        name = name.split("(", 1)[0].strip()
    parts = name.replace(",", " ").split()
    if len(parts) > 1 and parts[0].lower() == "the":  # "The Carlyle Group" -> "Carlyle"
        parts = parts[1:]
    while len(parts) > 1 and parts[-1].strip(".&").lower() in _INFORMAL_SUFFIXES:
        parts.pop()
    return " ".join(parts) or name

# One specific pain/value per persona, distilled from the datacenterHawk overview deck
# ("How <persona> Use datacenterHawk"). This is CONTEXT for the model to turn into ONE
# short, specific sentence in Email 1 — not a feature list to dump.
PERSONA_VALUE = {
    "investor": ("they underwrite and defend data center deals - we help them build the IC thesis on "
                 "analyst-verified capacity, pricing, and absorption data, and flag market risk in a "
                 "portfolio before it becomes a surprise."),
    "provider": ("they need to see competitive supply and where hyperscalers are leasing in their markets "
                 "without the manual lift, and qualify expansion sites on power and fiber before committing capital."),
    "vendor": ("they want to find operators building and expanding in their territory before the RFP lands, "
               "and rank accounts by capacity under construction."),
    "user": ("they screen markets and qualify power, fiber, and site infrastructure across deployment "
             "regions before the RFP goes out."),
    "broker": ("they win mandates and deliver client-ready market analysis - capacity, absorption, pricing, "
               "and comps - from one searchable source."),
    "default": ("they need a clear read on data center supply, demand, pricing, and infrastructure across "
                "markets to make faster, better-informed decisions."),
}


def _force_short_followups(emails, company, contact_name):
    """Email 2 and Email 3 are FIXED short nudges Hank approved (mirroring the inbound
    sequence) — NOT the verbose generated paragraphs. Keep Email 1 as-is, replace E2/E3.
    See feedback_sequence_copy_is_prewritten."""
    fn = (contact_name or "").strip().split()[0] if (contact_name or "").strip() else "there"
    disp = informal_company(company)
    subj = f"Re: datacenterHawk + {disp}"
    e2 = {"subject": subj, "send_day": 4,
          "body": f"{fn} -\n\nMaking sure you saw this. Can I send you some times to connect?"}
    e3 = {"subject": subj, "send_day": 10,
          "body": (f"{fn} -\n\nDoing a final check-in here. Would still love to show you the "
                   f"platform if you're open to it. Want to find a time?")}
    e1 = emails[0] if emails else {"subject": f"datacenterHawk + {disp}", "body": "", "send_day": 0}
    return [e1, e2, e3]


def draft_email_sequence(company, contact, persona=None, confluence_intel=None, email_examples=None, signal=None):
    """Generate a 3-email sequence using Claude Opus 4.6, grounded in Hank's actual voice.
    `signal` is the news article headline/summary that triggered this outreach - the email MUST
    open by referencing it. Pass None only when there truly is no hook (generic intro fallback)."""
    tone = load_tone_guide()
    disp = informal_company(company)  # informal name for subject + body (e.g. "Holder", not "Holder Construction")
    contact_name = contact.get("first_name") or contact.get("name", "").split()[0] if contact.get("name") else "there"
    contact_title = contact.get("title", "")
    # NOTE: Confluence research is INTERNAL ONLY - proprietary data.
    # Do NOT reference Confluence intel in emails. It's for our context only.
    # Emails should only reference what DCH's platform actually offers
    # and publicly available info about the prospect.
    intel_section = ""

    # Build email examples - most important input for voice matching
    example_section = ""
    if email_examples:
        example_section = "\nHANK'S ACTUAL SENT EMAILS (match this voice EXACTLY):\n"
        for i, e in enumerate(email_examples[:5]):
            text = e.get("text", "") if isinstance(e, dict) else str(e)
            example_section += f"\nExample {i+1}:\n{text[:500]}\n"

    # Fallback to local vault emails if Qdrant was unavailable
    if not example_section:
        local_examples = load_local_email_examples(persona)
        if local_examples:
            example_section = "\nHANK'S ACTUAL SENT EMAILS (from vault):\n"
            for i, ex in enumerate(local_examples[:5]):
                example_section += f"\nExample {i+1}:\n{ex}\n"
            log(f"Using {len(local_examples)} local email examples (Qdrant unavailable)")

    if not example_section:
        log("WARNING: No email examples available - draft quality will be lower")

    persona_label = f" ({persona})" if persona else ""

    signal_section = ""
    if signal:
        signal_section = (
            f"\nSPECIFIC SIGNAL (this is what triggered the outreach - EMAIL 1 MUST OPEN BY REFERENCING IT):\n"
            f"  {signal}\n\n"
            "The first line of Email 1 after the greeting must reference this signal specifically - "
            "what happened, what they did, what was announced. Treat it as the hook. If the signal is thin "
            "('fiber buildout milestone'), still anchor Email 1 to it: 'Saw the news on your fiber buildout...'. "
            "Do not write a generic intro when a signal is provided.\n"
        )
    else:
        signal_section = (
            "\nNO SIGNAL PROVIDED - lead Email 1 with a plain, direct intro tied to their role/title. "
            "Do not invent a signal. Do not say 'saw your announcement' or 'saw your expansion' without one.\n"
        )

    persona_value = PERSONA_VALUE.get((persona or "").lower().strip(), PERSONA_VALUE["default"])
    first = contact_name if contact_name else "there"

    prompt = (
        "You are ghostwriting ONE cold outbound email (Email 1) AS Hank Autrey, business development "
        "at datacenterHawk (a data center market-intelligence platform).\n\n"
        f"TARGET: {contact_name} ({contact_title}) at {disp}{persona_label}\n"
        f"  (Refer to the company informally as \"{disp}\" in both the subject and the body - never tack on "
        "generic suffixes like Construction/Infrastructure/Group.)\n"
        f"{signal_section}\n"
        "STUDY THESE REAL EMAILS HANK SENT. Your draft must be indistinguishable from them in length, "
        "rhythm, warmth, and informality. If your draft is longer, tidier, or more corporate than these, "
        "it is WRONG - rewrite it shorter and looser.\n"
        f"{example_section}\n\n"
        "HANK'S GOLD-STANDARD EMAIL 1 (match this exact feel - casual, plain, personal, social-proof close):\n"
        '"David - know you guys are expanding into the gpu and ai-compute asset backed financing space for '
        "neoclouds and operators. You'll probably find it hard to get really accurate and clear capacity and "
        "pricing info. Our space is pretty opaque right now. We can be that data source for you. Would you be "
        'interested in taking a look? Every major operator/investor in the data center space uses us."\n\n'
        "HOW HANK ACTUALLY WRITES EMAIL 1 (his shape - vary the wording, never make it longer or tidier):\n"
        f"  1. Greeting \"{first} -\" then go straight in, casually. The opener can start LOWERCASE (\"know you "
        "guys are...\", \"saw you...\") and drop the leading \"I\". Reference the signal / their recent work "
        "like a human who actually noticed. If NO signal, open with a short genuine observation - never a formula, never invented news.\n"
        "  2. ONE plain sentence naming a DIFFICULTY they'll hit, as a HEDGED, market-grounded prediction "
        "(\"you'll probably find it hard to get accurate capacity and pricing data - the space is pretty opaque "
        "right now\"). This is allowed. What is BANNED: claiming their internal priorities or role-stake "
        "(\"I imagine you care about X\", \"with you leading Y that's especially important\"). And KILL corporate "
        "jargon - no \"IC thesis\", \"catch market risk before it bites\", \"capital allocation\", \"analyst-verified\", "
        f"\"leverage\". (Internal context on this {persona or 'prospect'}'s world - TRANSLATE to plain casual words, never "
        f"lift its phrasing: {persona_value})\n"
        "  3. ONE dead-simple value line: \"we can be that data source for you\" / \"that's exactly the gap we fill\". "
        "Do NOT dump a capability/feature list.\n"
        "  4. Prefer SOCIAL PROOF over specs: \"Every major operator/investor in the data center space uses us\" or "
        f"\"firms like <2-3 relevant names> use us\". Use the long capability line ({HANK_CAPABILITY_LINE!r}) ONLY if it "
        "genuinely fits - usually skip it.\n"
        "  5. Soft CTA: \"Would you be interested in taking a look?\" or \"Can I send you some times to connect?\" "
        "Then \"Hank\" on its own line.\n\n"
        "VOICE: short, concise, informal, human. Contractions. A little loose is good (\"you guys\", lowercase "
        "\"spac\"). No paragraph over 2 sentences. Total ~60-90 words. Sound like a busy person who knows the "
        "space dashed off a quick, specific note - NOT marketing copy.\n\n"
        "HARD RULES:\n"
        f"  - Subject EXACTLY: \"datacenterHawk + {disp}\"\n"
        "  - BANNED openers/phrases: \"I imagine {company} cares a lot about...\", \"with you leading X...\", "
        "\"that's especially important to you / top of mind\", \"That's exactly what we cover\", "
        "\"As <title> at <company>\", \"I hope this finds you well\", \"leading the charge\", \"strategic\", "
        "marketing superlatives, \"kind of/sort of our thing\".\n"
        "  - BANNED jargon (translate to plain words): \"IC thesis\", \"build the thesis\", \"catch market risk "
        "before it bites\", \"capital allocation\", \"analyst-verified\", \"underwrite and defend\", \"leverage\". "
        "If it could've come from any SaaS rep, start over.\n"
        "  - No em-dashes or en-dashes. Hyphen-minus only.\n"
        "  - No Calendly / booking / scheduler links.\n"
        "  - Only reference PUBLIC info or the provided signal. NEVER invent a deal, number, or news. When "
        "unsure, stay general rather than fabricate.\n"
        "  - datacenterHawk tracks ONLY: facilities, market capacity, vacancy, pricing, absorption, transaction "
        "comps, company portfolios, and infrastructure layers (fiber, power, gas). Never claim anything else.\n\n"
        f"EXTRA STYLE NOTES:\n{tone}\n\n"
        "OUTPUT: Return ONLY a JSON object (no markdown, no commentary):\n"
        f'{{"subject": "datacenterHawk + {disp}", "body": "{first} -\\n\\n...\\n\\nHank"}}'
    )

    # Claude Opus 4.6 via direct Anthropic API. Key resolution order:
    #   1) ANTHROPIC_API_KEY env var
    #   2) ~/.openclaw/config/api-keys.md (works under launchd / SSH on the remote)
    #   3) iCloud vault path (only works on Hank's local Mac with TCC permission)
    # On the remote, the iCloud path raises PermissionError under launchd which the outer
    # try/except was silently catching, so EVERY drafting call was falling back to canned
    # templates. The order below tries the openclaw path FIRST so launchd-fired runs work.
    try:
        import urllib.request, re as _re
        claude_key = os.environ.get("ANTHROPIC_API_KEY") or ""
        if not claude_key:
            for api_keys_path in [
                Path.home() / ".openclaw/config/api-keys.md",
                Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/hank-vault/config/api-keys.md",
            ]:
                try:
                    if api_keys_path.exists():
                        m = _re.search(r"## Anthropic[\s\S]*?API Key \(DCH Company\)[^`]*`([^`]+)`", api_keys_path.read_text())
                        if m:
                            claude_key = m.group(1).strip()
                            break
                except (PermissionError, OSError):
                    continue

        if not claude_key:
            log("No Claude API key - falling back to templates")
            return draft_template_emails(company, contact_name, contact_title, persona)

        # Email 1 is generated by Claude Opus (best voice fidelity). Email 2/3 are the fixed
        # short nudges (_force_short_followups), so we only ask Opus for Email 1.
        DRAFT_MODEL = "claude-opus-4-8"
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({
                "model": DRAFT_MODEL,
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}]
            }).encode(),
            headers={
                "x-api-key": claude_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        resp = urllib.request.urlopen(req, context=SSL_CTX, timeout=90)
        result = json.loads(resp.read())
        text = result["content"][0]["text"].strip()
        if text.startswith("```"):
            text = _re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = _re.sub(r"\n?```$", "", text).strip()

        e1 = None
        try:
            e1 = json.loads(text)
        except Exception:
            mobj = _re.search(r'\{.*\}', text, _re.DOTALL)
            if mobj:
                try:
                    e1 = json.loads(mobj.group())
                except Exception:
                    e1 = None
        if not isinstance(e1, dict) or not e1.get("body"):
            log(f"{DRAFT_MODEL} response not valid Email-1 JSON - falling back to templates")
            return draft_template_emails(company, contact_name, contact_title, persona)

        e1.setdefault("subject", f"datacenterHawk + {disp}")
        e1["send_day"] = 0
        log(f"Email 1 drafted by {DRAFT_MODEL}")
        return {"emails": _force_short_followups([e1], company, contact_name), "model": DRAFT_MODEL}
    except Exception as e:
        log(f"Opus drafting failed ({e}) - falling back to templates")
        return draft_template_emails(company, contact_name, contact_title, persona)


def draft_template_emails(company, contact_name, contact_title, persona=None):
    """Generate emails from Hank's actual sent email patterns."""
    import random
    _disp = informal_company(company)  # informal name for subject lines


    # Voice-correct fallback (no "I imagine {company} cares about..." formula — BANNED, see
    # feedback_hank_outbound_voice: never assert the prospect's pain). There's no signal in the
    # template path, so open with a clean persona-specific value line in Hank's voice. E2/E3 are
    # forced to the short nudges by _force_short_followups, so e2/e3 below are placeholders.
    first = (contact_name or "there").split()[0] if (contact_name or "").strip() else "there"
    _openers = {
        "investor": "When you're underwriting and defending data center deals, having analyst-verified capacity, pricing, and absorption data to back the thesis makes a real difference.",
        "provider": "Keeping a clean read on competitive supply and where hyperscalers are leasing in your markets, without the manual lift, is tough to do well.",
        "vendor": "Finding the operators building and expanding in your territory before the RFP lands is half the battle.",
        "user": "Screening markets and qualifying power, fiber, and site infrastructure before an RFP goes out is a lot to pull together.",
        "broker": "Pulling together client-ready market reads, capacity, absorption, pricing, comps, usually means digging through ten different places.",
    }
    _op = _openers.get((persona or "").lower().strip(),
                       "Getting a clean read on data center supply, pricing, and infrastructure across markets is harder than it should be.")
    e1 = (f"{first} -\n\n{_op} That's what we do at datacenterHawk. {HANK_CAPABILITY_LINE}\n\n"
          "Can I send you some times to connect?\n\nHank")
    e2 = ""  # overwritten by _force_short_followups
    e3 = ""  # overwritten by _force_short_followups

    emails = [
        {"subject": f"datacenterHawk + {_disp}", "body": e1, "send_day": 0},
        {"subject": f"Re: datacenterHawk + {_disp}", "body": e2, "send_day": 4},
        {"subject": f"Re: datacenterHawk + {_disp}", "body": e3, "send_day": 10},
    ]
    return {"emails": _force_short_followups(emails, company, contact_name), "model": "template"}


# --- Pipeline Orchestrator ---
def process_prospect(company, domain=None, persona=None, signal=None):
    """Full pipeline for a single prospect. `signal` = the news headline/summary that triggered outreach."""
    log(f"\n{'='*60}")
    log(f"Processing: {company} ({domain or 'no domain'})")
    if signal:
        log(f"Signal: {signal[:100]}")

    result = {
        "company": company,
        "domain": domain,
        "persona": persona,
        "signal": signal,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "roe": None,
        "contact": None,
        "emails": None,
        "status": None,
        "confluence_intel": [],
    }

    # Check exclusion list
    if is_excluded(company, domain):
        result["status"] = "EXCLUDED"
        print(f"⏭️  {company} - EXCLUDED (in exclusion list)")
        return result

    # Check already-processed list (bypass with --force).
    # 30-day cooldown — prospects re-enter the funnel after that, since their fit + ROE
    # may have changed (new signal, activity died down, etc). Without the cooldown the
    # pipeline either re-processes EVERY run (wasted Opus calls + duplicated cards in
    # Slack/Telegram) or never reconsiders a prospect even when months have passed.
    processed_file = STATE_DIR / "processed-prospects.json"
    COOLDOWN_DAYS = 30
    if processed_file.exists() and "--force" not in sys.argv:
        try:
            processed = json.loads(processed_file.read_text())
            entry = processed.get(company.lower().strip())
            if entry:
                from datetime import datetime as _dt
                proc_iso = entry.get("processed_date") or ""
                age_days = None
                try:
                    proc_dt = _dt.fromisoformat(proc_iso.replace("Z", "+00:00"))
                    age_days = (_dt.now(proc_dt.tzinfo) - proc_dt).days
                except Exception:
                    age_days = None
                if age_days is None or age_days < COOLDOWN_DAYS:
                    last_status = entry.get("status", "?")
                    age_str = f"{age_days}d ago" if age_days is not None else "unknown date"
                    result["status"] = f"ALREADY_PROCESSED ({age_str}, was {last_status})"
                    log(f"Skipping {company} - processed {age_str}, last status={last_status}")
                    return result
                log(f"Re-processing {company} - {age_days} days since last attempt (>{COOLDOWN_DAYS}d cooldown elapsed)")
        except Exception:
            pass

    # Step 1: ROE Check
    print(f"\n🔍  Checking ROE: {company}...")
    roe = run_roe_check(company, domain)
    result["roe"] = roe

    if not roe.get("status", "").startswith("CLEAR"):
        result["status"] = f"BLOCKED ({roe.get('status_detail', '?')})"
        owner = roe.get("owner", "?")
        reasons = roe.get("blocking_reasons", [])
        print(f"🚫  {company} - BLOCKED")
        for r in reasons[:3]:
            print(f"     {r}")
        print(f"     Owner: {owner}")
        return result

    print(f"✅  {company} - CLEAR ({roe.get('status_detail', '')})")

    # Get SF account ID for contact lookup
    sf_account_id = None
    if roe.get("accounts"):
        sf_account_id = roe["accounts"][0].get("id")

    # Step 1b: Bootstrap domain via Apollo if signal didn't include one
    # (without this, Layer 2 fit scoring + Hunter email finder both skip and we lose the fit_score)
    if not domain:
        bootstrapped = apollo_org_search(company)
        if bootstrapped:
            domain = bootstrapped
            result["domain"] = domain
            print(f"🔎  Domain bootstrapped via Apollo: {domain}")

    # Step 2: Research the company - what do they actually do? Are they a fit?
    if domain:
        print(f"🔬  Researching {company}...")
        # Forward the surfacing signal into fit scoring. Without it the scorer judges
        # only the stale Apollo description and silently SKIPs prospects flagged BY a
        # data-center signal (e.g. Optio's GPU-financing pivot scoring 2/10 on a
        # consumer-asset-finance blurb). The scorer treats the signal as current evidence.
        research_cmd = ["python3", str(Path.home() / ".local/bin/company-research.py"), company, domain, "--json"]
        if signal:
            research_cmd.extend(["--signal", signal])
        research_result = subprocess.run(
            research_cmd,
            capture_output=True, text=True, timeout=30,
        )
        try:
            research = json.loads(research_result.stdout)
            result["research"] = research
            fit_score = research.get("fit_score", 5)
            research_persona = research.get("persona", "unknown")
            recommendation = research.get("recommendation", "REVIEW")
            description = research.get("description", "")[:150]

            print(f"   Persona: {research_persona} | Fit: {fit_score}/10 | {recommendation}")
            if description:
                print(f"   {description}")

            # Use researched persona instead of passed-in guess
            if research_persona != "unknown":
                persona = research_persona
                result["persona"] = persona

            # Skip if not a fit (unless --force)
            if recommendation == "SKIP" and "--force" not in sys.argv:
                result["status"] = f"SKIPPED (fit_score={fit_score}, {research.get('fit_reason', '')})"
                print(f"❌  {company} - NOT A FIT (score {fit_score}/10)")
                return result
            elif recommendation == "SKIP":
                print(f"⚠️  {company} - fit_score={fit_score}/10 but --force set, continuing")

        except Exception as e:
            log(f"Research parse error: {e}")

    # Step 4: Find Contact
    print(f"👤  Finding contact at {company}...")
    contact = run_contact_finder(company, domain, persona, sf_account_id)
    result["contact"] = contact

    best = contact.get("best_contact")
    if not best or not best.get("email"):
        result["status"] = "CLEAR_NO_CONTACT"
        if best:
            print(f"⚠️  Found {best.get('name', '?')} ({best.get('title', '?')}) but no email")
            print(f"     LinkedIn: {best.get('linkedin_url', 'none')}")
            print(f"     Action: Manual email discovery needed")
        else:
            print(f"❌  No contact found - needs manual prospecting")
        return result

    print(f"✅  Contact: {best['name']} ({best.get('title', '?')})")
    print(f"     Email: {best['email']} ({contact.get('email_source', '?')})")

    # Step 4b: Ensure contact exists in Salesforce (required for EAC email sync)
    if sf_account_id and best.get("email"):
        ensure_sf_contact(sf_account_id, best, company)

    # Step 5: Search email vectors for tone matching (query tuned to company+signal when available)
    signal = result.get("signal") or ""
    tone_query = signal if signal else f"cold outreach {company} {persona or 'provider'} data center introduction"
    log("Searching email vectors for tone matching...")
    email_examples = search_email_vectors(tone_query, limit=5)

    # Step 5: Draft Email Sequence
    print(f"✉️  Drafting 3-email sequence...")
    emails = draft_email_sequence(
        company, best, persona,
        confluence_intel=result["confluence_intel"],
        email_examples=email_examples,
        signal=signal or None,
    )
    result["emails"] = emails

    if emails.get("emails"):
        result["status"] = "READY"
        print(f"✅  Sequence ready ({emails.get('model', '?')} model)")
        for i, e in enumerate(emails["emails"]):
            print(f"     Email {i+1} (Day {e.get('send_day', '?')}): {e.get('subject', '?')}")
    else:
        result["status"] = "CLEAR_NO_DRAFT"
        print(f"⚠️  Drafting failed: {emails.get('error', '?')}")

    return result


def save_pipeline_result(result):
    """Save result to pipeline state."""
    ensure_dirs()
    state = {}
    if PROSPECTS_FILE.exists():
        state = json.loads(PROSPECTS_FILE.read_text())

    key = result["company"].lower().strip()
    state[key] = result
    PROSPECTS_FILE.write_text(json.dumps(state, indent=2, default=str))

    # Also save sequence to individual file if emails exist
    if (result.get("emails") or {}).get("emails"):
        slug = re.sub(r'[^a-z0-9]+', '-', result["company"].lower())[:40]
        seq_file = SEQUENCE_DIR / f"{slug}.json"
        seq_data = {
            "company": result["company"],
            "domain": result.get("domain"),
            "contact": result.get("contact", {}).get("best_contact"),
            "emails": result["emails"]["emails"],
            "created": result["processed_at"],
            "status": "pending_approval",
        }
        seq_file.write_text(json.dumps(seq_data, indent=2, default=str))


def print_sequence(result):
    """Print the full email sequence for review."""
    emails = result.get("emails", {}).get("emails", [])
    contact = result.get("contact", {}).get("best_contact", {})
    if not emails:
        return

    print(f"\n{'='*60}")
    print(f"SEQUENCE: {result['company']}")
    print(f"TO: {contact.get('name', '?')} <{contact.get('email', '?')}>")
    print(f"TITLE: {contact.get('title', '?')}")
    print(f"{'='*60}")

    for i, e in enumerate(emails):
        print(f"\n--- Email {i+1} (Day {e.get('send_day', '?')}) ---")
        print(f"Subject: {e.get('subject', '?')}")
        print(f"\n{e.get('body', '?')}")

    print(f"\n{'='*60}\n")


def batch_process(csv_path):
    """Process a batch of prospects from CSV."""
    import csv
    results = {"ready": [], "blocked": [], "no_contact": [], "errors": []}

    with open(csv_path) as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for row in reader:
            if not row or not row[0].strip():
                continue
            company = row[0].strip()
            domain = row[1].strip() if len(row) > 1 and row[1].strip() else None
            persona = row[2].strip() if len(row) > 2 and row[2].strip() else None
            signal  = row[3].strip() if len(row) > 3 and row[3].strip() else None

            try:
                result = process_prospect(company, domain, persona, signal=signal)
                save_pipeline_result(result)

                # Add to processed-prospects list so we never re-process
                try:
                    processed_file = STATE_DIR / "processed-prospects.json"
                    processed = json.loads(processed_file.read_text()) if processed_file.exists() else {}
                    processed[result["company"].lower().strip()] = {
                        "company": result["company"],
                        "status": result.get("status", "?"),
                        "processed_date": result.get("processed_at", ""),
                    }
                    processed_file.write_text(json.dumps(processed, indent=2))
                except Exception:
                    pass

                if result["status"] == "READY":
                    print_sequence(result)
                    results["ready"].append(result)
                elif result["status"].startswith("BLOCKED"):
                    results["blocked"].append(result)
                elif "NO_CONTACT" in (result.get("status") or ""):
                    results["no_contact"].append(result)
                else:
                    results["errors"].append(result)
            except Exception as e:
                print(f"❌  ERROR processing {company}: {e}")
                results["errors"].append({"company": company, "error": str(e)})

    print(f"\n{'='*60}")
    print(f"BATCH SUMMARY")
    print(f"  READY (sequence drafted): {len(results['ready'])}")
    print(f"  BLOCKED (ROE):            {len(results['blocked'])}")
    print(f"  NO CONTACT:               {len(results['no_contact'])}")
    print(f"  ERRORS:                   {len(results['errors'])}")
    print(f"{'='*60}")
    return results


def main():
    ensure_dirs()
    # Build positional args while skipping flag values. The previous one-liner
    # `[a for a in sys.argv[1:] if not a.startswith("-")]` filtered the flag itself
    # but kept the value that followed it - e.g. `--persona vendor` → "vendor" ended up
    # as args[1] and was treated as `domain`. That silently broke ROE for every
    # prospect run with persona but no domain (caused false drafts to NVIDIA,
    # Siemens Energy, Applied Digital, Veolia on the 2026-04-28 launchd run).
    FLAGS_WITH_VALUE = {"--persona", "--batch", "--exclude", "--signal"}
    args = []
    i = 1
    while i < len(sys.argv):
        a = sys.argv[i]
        if a in FLAGS_WITH_VALUE:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        args.append(a)
        i += 1

    if "--batch" in sys.argv:
        idx = sys.argv.index("--batch")
        csv_path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        if not csv_path:
            print("Usage: outbound-pipeline.py --batch /path/to/prospects.csv")
            sys.exit(1)
        batch_process(csv_path)
        return

    if "--exclude" in sys.argv:
        idx = sys.argv.index("--exclude")
        company = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        domain = sys.argv[idx + 2] if idx + 2 < len(sys.argv) and not sys.argv[idx + 2].startswith("-") else None
        if company:
            save_exclusion(company, domain, "manual")
            print(f"Added {company} to exclusion list")
        return

    if "--show-ready" in sys.argv:
        if PROSPECTS_FILE.exists():
            state = json.loads(PROSPECTS_FILE.read_text())
            ready = {k: v for k, v in state.items() if v.get("status") == "READY"}
            print(f"\nREADY prospects ({len(ready)}):")
            for k, v in ready.items():
                contact = v.get("contact", {}).get("best_contact", {})
                print(f"  {v['company']} → {contact.get('name', '?')} <{contact.get('email', '?')}>")
        return

    if len(args) < 1:
        print("Usage:")
        print("  outbound-pipeline.py 'Company' 'domain.com' [--persona investor] [--signal \"hook...\"]")
        print("  outbound-pipeline.py --batch /path/to/prospects.csv")
        print("  outbound-pipeline.py --exclude 'Company' ['domain.com']")
        print("  outbound-pipeline.py --show-ready")
        print("\nFlags: -v/--verbose, --no-draft (skip email drafting)")
        sys.exit(1)

    company = args[0]
    domain = args[1] if len(args) > 1 else None

    persona = None
    if "--persona" in sys.argv:
        idx = sys.argv.index("--persona")
        persona = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None

    # --signal "<hook>" lets a manual run pass the specific event Email 1 opens on
    # (e.g. a deal/coverage announcement). Auto/news runs already pass a signal internally.
    signal = None
    if "--signal" in sys.argv:
        idx = sys.argv.index("--signal")
        signal = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None

    # Qdrant is local on both machines (localhost:6333). If it's not running, vector search
    # will skip silently - no remote tunneling.
    try:
        check = subprocess.run(["lsof", "-ti", "tcp:6333"], capture_output=True, text=True)
        if not check.stdout.strip():
            log("Local Qdrant not running on 6333 - vector search will be skipped")
    except Exception:
        pass

    result = process_prospect(company, domain, persona, signal=signal)

    # When dedup gate fires, the result is a stub (just status="ALREADY_PROCESSED..."
    # without roe/contact/emails). Saving it to pipeline-results.json would WIPE the
    # rich previous record (roe.owner, contact, drafted emails) — daily report would
    # then show "owner: ?" everywhere. So skip the save AND skip the dedup-list write
    # in that case; the prior state is intact and the cooldown timestamp is unchanged.
    is_dedup_skip = (result.get("status") or "").startswith("ALREADY_PROCESSED")
    if not is_dedup_skip:
        save_pipeline_result(result)

        # Mirror what the batch loop does — record this prospect in processed-prospects.json
        # so the 30-day cooldown in process_prospect() actually fires on subsequent runs.
        # Single-mode (called from outbound-daily.py one company at a time) was missing this
        # write, which meant every daily run re-evaluated the same prospects from scratch
        # (2026-05-01 same-prospects-every-run incident).
        try:
            processed_file = STATE_DIR / "processed-prospects.json"
            processed = json.loads(processed_file.read_text()) if processed_file.exists() else {}
            processed[result["company"].lower().strip()] = {
                "company": result["company"],
                "status": result.get("status", "?"),
                "processed_date": result.get("processed_at", ""),
            }
            processed_file.write_text(json.dumps(processed, indent=2))
        except Exception:
            pass

    if result["status"] == "READY":
        print_sequence(result)
    elif "--no-draft" not in sys.argv and result["status"] and "CLEAR" in result["status"]:
        print(f"\nTip: Run with -v for more detail")


if __name__ == "__main__":
    main()
