#!/usr/bin/env python3
"""
outbound-daily.py — Daily autonomous outbound prospecting engine.

Runs every weekday morning. Pulls signals from multiple sources,
runs ROE checks, finds contacts, drafts emails, and posts to Telegram.

Signal Sources:
  1. Airtable "Inbound Intel" — DC news articles (new companies, expansions, funding)
  2. Competitor discovery — Find competitors of current customers via Apollo
  3. SF cold leads — Inbound leads that went cold (no activity in 60+ days)
  4. Confluence Research — New company pages added by the research team

Usage:
  python3 outbound-daily.py                    # Full daily run
  python3 outbound-daily.py --source airtable  # Single source only
  python3 outbound-daily.py --source competitors
  python3 outbound-daily.py --source sf-leads
  python3 outbound-daily.py --dry-run          # Scan only, no pipeline/Telegram
  python3 outbound-daily.py --max 5            # Limit prospects processed
"""

import json
import os
import subprocess
import sys
import ssl
import re
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

# --- Config ---
VAULT = Path.home() / ".openclaw/workspace"
STATE_DIR = VAULT / "outbound-v2"
DAILY_LOG_DIR = STATE_DIR / "daily-logs"
DAILY_STATE_FILE = STATE_DIR / "daily-state.json"
PROSPECTS_FILE = STATE_DIR / "pipeline-results.json"

APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "")
AIRTABLE_BASE = os.environ.get("AIRTABLE_BASE", "")

REMOTE = os.environ.get("REMOTE_HOST", "user@remote-host.example.com")

MAX_PROSPECTS = 999  # Default per run
DRY_RUN = "--dry-run" in sys.argv
VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

# Geographic filter
SKIP_COUNTRIES = {
    "india", "china", "japan", "south korea", "australia", "singapore",
    "malaysia", "indonesia", "thailand", "vietnam", "philippines",
    "taiwan", "hong kong",
}


# Hard blocklist — current customers and hyperscalers. NEVER prospect these.
KNOWN_CUSTOMERS = {
    "1547 realty",
    "165 halsey street",
    "acs infrastructure development, inc.",
    "ada infrastructure (fka glp)",
    "aes clean energy",
    "affinius capital (fka us realty co)",
    "alvarez and marsal",
    "amazon (aws)",
    "arcapita",
    "arthur d. little ltd",
    "ascenty",
    "atlas renewable",
    "avison young inc",
    "bain capital",
    "balanced rock power",
    "balfour beatty",
    "barclays capital inc",
    "barings",
    "bbg real estate services, inc.",
    "belimo",
    "blackrock inc",
    "bmo capital markets corp.",
    "border states",
    "bridgepointe technologies , inc.",
    "broadwaters capital llc",
    "brookfield asset management inc.",
    "canadian pension plan investment board (cppib)",
    "caprock partners",
    "cavalry real estate advisors",
    "cbre",
    "cim group",
    "cirion",
    "cleanarc data centers",
    "cloudhq",
    "colliers international ltd",
    "compass data centers",
    "consolidated communications",
    "constellation energy",
    "coresite realty corporation",
    "coreweave",
    "corning optical communications",
    "cpg",
    "cummins",
    "cushman & wakefield inc",
    "cyrusone llc",
    "darsana",
    "decennial group / trillium",
    "deloitte llp",
    "department of energy",
    "dh capital (citizens jmp securities)",
    "diamond realty investments",
    "digital realty inc",
    "directline",
    "dp electric",
    "dpr construction inc",
    "eastdil secured llc",
    "edgeconnex inc",
    "edged energy",
    "elea data centers (fka elea digital)",
    "element critical",
    "elliott management corporation",
    "engie",
    "entergy",
    "equinix inc",
    "ey-parthenon",
    "ferrovial",
    "flexential",
    "frontier infrastructure",
    "ftidelta",
    "gic",
    "google",
    "govlane",
    "graybar electric co, inc., a new york corporation",
    "green street advisors inc.",
    "greenscale data centres",
    "greensquare dc (fka ck property group)",
    "gridcare, inc.",
    "harrison street real estate capital llc",
    "highland properties manassas, llc",
    "i squared capital",
    "idemitsu",
    "iem",
    "intersect power inc",
    "invesco",
    "invesco advisers, inc.",
    "iron mountain",
    "jenbacher-innio",
    "jera americas",
    "jones lang lasalle - latam",
    "jones lang lasalle incorporated",
    "jp morgan",
    "kbc advisors",
    "king street",
    "kio networks (metro net hosting)",
    "krlp",
    "lambda",
    "legacy power capital (bormacc group - to update)",
    "link logistics real estate",
    "liquid load banks",
    "lone pine capital",
    "mangum economics",
    "menlo equities",
    "meta",
    "mgx",
    "michels construction inc",
    "microsoft corporation",
    "mizuho bank",
    "montera infrastructure",
    "morgan stanley & co.",
    "mtp sites",
    "nepi rockcastle",
    "network connex",
    "newmark knight frank",
    "nextera energy , inc.",
    "nexus data centers",
    "nira energy",
    "northeast natural energy",
    "ntt global data centers",
    "nvidia corporation",
    "oklo inc",
    "openai",
    "orca data",
    "panattoni",
    "partner esi",
    "peak plus energy",
    "pennybacker capital",
    "point72 asset management l.p",
    "pointstate capital lp",
    "power insights (fka won associates)",
    "powerhouse (american real estate partners)",
    "prime data centers",
    "principal financial group , inc.",
    "provident",
    "qts data centers",
    "qualitas energy",
    "quanta isg",
    "radius dc",
    "revitalization unlimited llc",
    "rk industries",
    "rowan digital infrastructure",
    "ryan, llc",
    "sage brush infra",
    "salute",
    "sb energy",
    "scala data centers sa",
    "sdc capital partners",
    "semianalysis",
    "sidewalk infrastructure partners (verrus)",
    "siemens",
    "sm plus",
    "socomec sas",
    "soda springs development company, llc",
    "solomon partners",
    "sony interactive entertainment",
    "southern company",
    "spectrum enterprise",
    "stack infrastructure",
    "staley point capital",
    "starwood partners",
    "stockland",
    "stonepeak partners l.p",
    "stratcap (fka strategic data center fund)",
    "stream data centers",
    "sub zero engineering",
    "swift digital development",
    "switch (us)",
    "t. dallas smith and co",
    "t. rowe price",
    "t5 data centers llc",
    "tecto",
    "telehouse eu",
    "telxius",
    "terra nova dc",
    "the blackchamber group",
    "the boston consulting group, inc",
    "tpg real estate",
    "tract (fleet)",
    "trammell crow company",
    "trivest advisors",
    "tvg consulting, llc",
    "ubs",
    "valley industrial partners",
    "valorc3 data centers",
    "vantage data centers",
    "volterra advisors",
    "wacona capital",
    "walker & dunlop",
    "woodline partners",
    "yancey bros",
    "yondr",
}

NOT_COMPANIES = {
    "iran", "poland", "america", "indian", "chinese", "swedish", "modular",
    "developer", "property", "energy shock", "texas water",
    "bernie sanders", "startup arbor", "festus", "sunbury", "pennsylvania house",
    "yahoo", "ryan", "galaxy", "the", "us", "ai", "arm", "new", "big",
    "eu", "uk", "french", "german", "british", "japanese", "korean",
    "australian", "canadian", "african", "european", "asian", "american",
    "senator", "governor", "mayor", "president", "congress", "house",
    "senate", "bill", "act", "report", "study", "survey", "forecast",
    "cloud", "blockchain", "crypto",
}


def is_obviously_not_a_company(name):
    """Quick check for names that are obviously not companies or are blocked."""
    key = name.lower().strip()
    if key in NOT_COMPANIES:
        return True
    if key in KNOWN_CUSTOMERS:
        return True
    if len(key.split()) == 1 and len(key) < 4:
        return True
    if key.endswith("'s") or key.endswith("\u2019s"):
        return True
    return False


def log(msg):
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_daily_state():
    if DAILY_STATE_FILE.exists():
        return json.loads(DAILY_STATE_FILE.read_text())
    return {
        "last_run": None,
        "competitor_search_index": 0,  # Rotate through customers
        "sf_lead_offset": 0,
    }


def save_daily_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    DAILY_STATE_FILE.write_text(json.dumps(state, indent=2))


def load_current_customers():
    try:
        import openpyxl
        wb = openpyxl.load_workbook(
            VAULT / "current-customers/Current Customers-2026-03-09-09-18-41.xlsx"
        )
        ws = wb.active
        customers = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            vals = [str(v or "").strip() for v in row]
            if "Current Customer" in vals and len(vals) > 3 and vals[3]:
                name = vals[3]
                website = vals[4] if len(vals) > 4 else ""
                customers.append({"name": name, "website": website})
        return customers
    except Exception:
        return []


def load_already_processed():
    if PROSPECTS_FILE.exists():
        try:
            return set(json.loads(PROSPECTS_FILE.read_text()).keys())
        except OSError:
            import time; time.sleep(2)
            return set(json.loads(PROSPECTS_FILE.read_text()).keys())
    return set()


def load_exclusions():
    ef = STATE_DIR / "exclusion-list.json"
    if ef.exists():
        data = json.loads(ef.read_text())
        return {e.get("company", "").lower() for e in data.get("companies", [])}
    return set()


def is_geo_filtered(country):
    """Check if a country should be skipped."""
    if not country:
        return False
    return country.lower().strip() in SKIP_COUNTRIES


def sf_query(soql):
    result = subprocess.run(
        ["sf", "data", "query", "--query", soql, "--json"],
        capture_output=True, text=True, timeout=30
    )
    try:
        data = json.loads(result.stdout)
        return data.get("result", {}).get("records", [])
    except Exception:
        return []


# --- Signal Source 1: Airtable News ---
def scan_airtable(days=2):
    """Pull new high-relevancy articles from Airtable."""
    log("Source 1: Airtable news scan...")
    result = subprocess.run(
        ["python3", "-W", "ignore", str(Path.home() / ".local/bin/signal-scan.py"),
         "--days", str(days), "--min-relevancy", "3", "--output", "json"],
        capture_output=True, text=True, timeout=60,
    )
    try:
        # JSON output may have warnings before it — find the JSON array
        stdout = result.stdout.strip()
        json_start = stdout.find("[")
        if json_start >= 0:
            stdout = stdout[json_start:]
        prospects = json.loads(stdout)
        log(f"  Airtable: {len(prospects)} raw prospects")
        return [
            {
                "company": p["company"],
                "domain": p.get("domain"),
                "persona": p.get("persona", "provider"),
                "source": "airtable_news",
                "signal": p.get("articles", [{}])[0].get("title", "")[:80] if p.get("articles") else "",
            }
            for p in prospects[:20]  # Cap at 20
        ]
    except Exception as e:
        log(f"  Airtable scan error: {e}")
        return []


# --- Signal Source 2: Competitor Discovery ---
def find_competitors(max_searches=3):
    """Find competitors of current customers via Apollo industry search."""
    log("Source 2: Competitor discovery...")
    customers = load_current_customers()
    if not customers:
        log("  No customer list available")
        return []

    state = load_daily_state()
    start_idx = state.get("competitor_search_index", 0) % len(customers)

    # Pick a few customers to find competitors for (rotate daily)
    batch = customers[start_idx:start_idx + max_searches]
    if len(batch) < max_searches:
        batch += customers[:max_searches - len(batch)]

    prospects = []
    processed = load_already_processed()
    exclusions = load_exclusions()

    for customer in batch:
        name = customer["name"]
        website = customer.get("website", "")
        domain = ""
        if website:
            domain = re.sub(r'https?://(www\.)?', '', website).split('/')[0]

        if not domain:
            continue

        log(f"  Searching competitors of {name} ({domain})...")

        # Use Apollo org enrich to get the customer's industry keywords,
        # then search for other companies with similar keywords
        try:
            # Step 1: Get the customer's org keywords from Apollo
            org_payload = json.dumps({"domain": domain}).encode()
            org_req = urllib.request.Request(
                "https://api.apollo.io/api/v1/organizations/enrich",
                data=org_payload,
                headers={"Content-Type": "application/json", "X-Api-Key": APOLLO_API_KEY, "User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(org_req, timeout=15, context=SSL_CTX) as org_resp:
                org_data = json.loads(org_resp.read())

            org_info = org_data.get("organization", {})
            org_keywords = org_info.get("keywords", [])[:5]
            org_industry = org_info.get("industry", "")

            # Build search keywords — use DC-relevant keywords from the customer,
            # or fall back to generic DC terms
            dc_relevant = [kw for kw in org_keywords if any(
                term in kw.lower() for term in
                ["data center", "colocation", "hyperscale", "digital infrastructure",
                 "wholesale", "infrastructure", "colo", "cloud", "power", "energy",
                 "investment", "real estate", "broker"]
            )]

            if dc_relevant:
                search_kw = " ".join(dc_relevant[:3])
            elif org_industry and "data center" in org_industry.lower():
                search_kw = org_industry
            else:
                # Skip this customer — their Apollo profile isn't DC-relevant
                log(f"    Skipping {name} — no DC-relevant keywords")
                continue

            log(f"    DC keywords from {name}: {search_kw[:60]}")

            # Step 2: Search for people at companies with similar keywords
            import random
            page = random.randint(1, 10)

            payload = json.dumps({
                "q_keywords": search_kw,
                "person_titles": ["VP Business Development", "VP Sales", "Chief Commercial Officer", "Managing Director", "VP Strategy"],
                "person_locations": ["United States", "United Kingdom", "Germany", "France", "Canada", "Netherlands"],
                "organization_num_employees_ranges": ["51,200", "201,500", "501,1000", "1001,5000", "5001,10000"],
                "page": page,
                "per_page": 25,
            }).encode()
            req = urllib.request.Request(
                "https://api.apollo.io/api/v1/mixed_people/search",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Api-Key": APOLLO_API_KEY,
                    "User-Agent": "Mozilla/5.0",
                },
            )
            with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
                result = json.loads(resp.read())

            customer_names = {c["name"].lower() for c in customers}

            for person in result.get("people", []):
                org = person.get("organization") or {}
                comp_name = org.get("name", "")
                comp_domain = org.get("primary_domain", "")
                comp_country = org.get("country", "")
                comp_key = comp_name.lower().strip()

                # Filter
                if not comp_name or comp_key in processed or comp_key in exclusions:
                    continue
                if comp_key in customer_names:
                    continue
                if is_geo_filtered(comp_country):
                    continue
                if comp_domain == domain:
                    continue

                prospects.append({
                    "company": comp_name,
                    "domain": comp_domain,
                    "persona": "provider",
                    "source": "competitor_discovery",
                    "signal": f"Similar to {name} (keywords: {search_kw[:40]})",
                })

        except Exception as e:
            log(f"  Apollo competitor search error: {e}")

    # Update rotation index
    state["competitor_search_index"] = (start_idx + max_searches) % max(len(customers), 1)
    save_daily_state(state)

    log(f"  Found {len(prospects)} competitor prospects")
    return prospects[:10]


# --- Signal Source 3: SF Cold Leads ---
def scan_sf_cold_leads(max_leads=10):
    """Find inbound SF leads that went cold — no activity in 60+ days."""
    log("Source 3: SF cold leads...")

    cutoff_60 = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%dT00:00:00Z")
    cutoff_180 = (datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%dT00:00:00Z")

    # Find leads created 60-180 days ago with no recent activity
    records = sf_query(
        f"SELECT Id, Name, Company, Email, Title, Status, OwnerId, Owner.Name, CreatedDate "
        f"FROM Lead "
        f"WHERE CreatedDate >= {cutoff_180} "
        f"AND CreatedDate <= {cutoff_60} "
        f"AND Status != 'Qualified' "
        f"AND Status != 'Converted' "
        f"AND IsConverted = false "
        f"AND Company != null "
        f"AND Company != 'Unknown' "
        f"AND Company != 'Created by Zapier' "
        f"AND Company != 'Test' "
        f"AND Email != null "
        f"ORDER BY CreatedDate DESC "
        f"LIMIT {max_leads * 3}"
    )

    processed = load_already_processed()
    exclusions = load_exclusions()
    prospects = []

    seen_companies = set()
    for r in records:
        company = r.get("Company", "").strip()
        email = r.get("Email", "")
        company_key = company.lower()

        if not company or company_key in processed or company_key in exclusions:
            continue
        if company_key in seen_companies:
            continue

        # Extract domain from email
        domain = email.split("@")[1] if "@" in email else None
        if domain and any(d in domain for d in ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com"]):
            domain = None  # Skip personal email domains

        seen_companies.add(company_key)
        prospects.append({
            "company": company,
            "domain": domain,
            "persona": None,  # Will be auto-detected
            "source": "sf_cold_lead",
            "signal": f"Inbound lead from {r.get('CreatedDate', '?')[:10]}, status: {r.get('Status', '?')}",
        })

        if len(prospects) >= max_leads:
            break

    log(f"  Found {len(prospects)} cold lead prospects")
    return prospects


# --- Orchestrator ---
def run_pipeline_for_prospect(prospect):
    """Run the full pipeline for a single prospect."""
    company = prospect["company"]
    domain = prospect.get("domain") or ""
    persona = prospect.get("persona") or ""

    cmd = ["python3", str(Path.home() / ".local/bin/outbound-pipeline.py"), company]
    if domain:
        cmd.append(domain)
    if persona:
        cmd.extend(["--persona", persona])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return result.stdout, result.returncode


def post_to_approval(company):
    """Post a READY prospect to Telegram + create Outlook draft."""
    result = subprocess.run(
        ["python3", str(Path.home() / ".local/bin/outbound-approve.py"),
         "--post", company],
        capture_output=True, text=True, timeout=60,
    )
    return result.stdout


def check_followups():
    """Check and create due follow-up drafts."""
    result = subprocess.run(
        ["python3", str(Path.home() / ".local/bin/outbound-sequence.py"), "--check", "--send"],
        capture_output=True, text=True, timeout=600,
    )
    return result.stdout


def notify_telegram(message):
    """Post daily summary to Slack #outbound-hank for Rudy to forward."""
    SLACK_TOKEN = os.environ.get("SLACK_TOKEN", "")
    try:
        subprocess.run(
            ["curl", "-s", "-X", "POST", "https://slack.com/api/chat.postMessage",
             "-H", f"Authorization: Bearer {SLACK_TOKEN}",
             "-H", "Content-Type: application/json",
             "-d", json.dumps({"channel": "C0000000000", "text": message})],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def fit_filter(prospects):
    """Use Claude Opus 4.6 (direct Anthropic) to batch-evaluate which prospects are good fits."""
    if not prospects:
        return []

    # First pass: remove obvious non-companies and known customers
    filtered = [p for p in prospects if not is_obviously_not_a_company(p["company"])]
    removed = len(prospects) - len(filtered)
    if removed > 0:
        log(f"  Removed {removed} non-companies/known customers")

    if not filtered:
        return []

    # Build the list for LLM evaluation
    company_list = []
    for i, p in enumerate(filtered):
        company_list.append(f"{i+1}. {p['company']} -- Signal: {p.get('signal', 'N/A')[:80]}")

    prompt = f"""Evaluate companies for outbound sales prospecting for datacenterHawk, a market intelligence platform tracking data center projects across ~400 markets globally.

Our customers are: DC providers/operators, investors in DC infrastructure, brokers/consultants, vendors/contractors, and enterprises building their own DCs.

For each company, respond with ONLY the number and YES or NO.
YES = could be a datacenterHawk customer.
NO = not a fit (too small, not in DC industry, already a massive hyperscaler like AWS/Google/Microsoft/Meta that we already work with, a government entity, a news topic and not a company, or not actually a company name).

Companies:
{chr(10).join(company_list)}

Format:
1. YES
2. NO
..."""

    try:
        # Load Anthropic key from vault api-keys.md (iCloud) or remote openclaw path
        def _load_anthropic_key():
            import re as _re
            from pathlib import Path as _Path
            for p in [_Path.home()/".openclaw/config/api-keys.md",
                      _Path.home()/"Library/Mobile Documents/com~apple~CloudDocs/hank-vault/config/api-keys.md"]:
                try:
                    if p.exists():
                        m = _re.search(r"## Anthropic[\s\S]*?API Key \(DCH Company\)[^`]*`([^`]+)`", p.read_text())
                        if m: return m.group(1).strip()
                except Exception: pass
            return os.environ.get("ANTHROPIC_API_KEY") or ""
        ANTHROPIC_KEY = _load_anthropic_key()
        payload = json.dumps({
            "model": "claude-opus-4-8",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()

        import urllib.request, ssl
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ctx = ssl.create_default_context()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            result = json.loads(resp.read())

        response_text = result.get("content", [{}])[0].get("text", "")

        # Parse YES/NO responses
        passed = []
        for line in response_text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split(".", 1)
            if len(parts) == 2:
                idx_str = parts[0].strip()
                answer = parts[1].strip().upper()
                try:
                    idx = int(idx_str) - 1
                    if 0 <= idx < len(filtered) and answer.startswith("YES"):
                        passed.append(filtered[idx])
                except (ValueError, IndexError):
                    continue

        log(f"  Fit filter: {len(passed)} passed out of {len(filtered)}")
        return passed if passed else filtered[:max(5, len(filtered))]

    except Exception as e:
        log(f"  Fit filter error: {e} -- using all prospects")
        return filtered



def daily_run(sources=None, max_prospects=None):
    """Full daily outbound run."""
    if max_prospects is None:
        max_prospects = MAX_PROSPECTS

    start_time = datetime.now()
    today = start_time.strftime("%Y-%m-%d")

    log(f"{'='*60}")
    log(f"DAILY OUTBOUND RUN — {today}")
    log(f"{'='*60}")

    # Ensure dirs
    DAILY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Open SSH tunnel for vector search
    tunnel = None
    try:
        check = subprocess.run(["lsof", "-ti", "tcp:6333"], capture_output=True, text=True)
        if not check.stdout.strip():
            log("Opening SSH tunnel to Qdrant...")
            tunnel = subprocess.Popen(
                ["ssh", "-N", "-L", "6333:localhost:6333", REMOTE],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            import time; time.sleep(2)
    except Exception:
        pass

    try:
        # Step 1 moved to end — sequence checks are slow with many contacts
        # Tracking stats for summary
        feeds_polled = 0
        feeds_total = 23
        new_articles = 0
        airtable_count = 0
        pre_filter_removed = 0
        fit_filter_passed = 0
        roe_checked = 0
        roe_blocked = 0
        contacts_found = 0
        drafts_created = 0

        # Step 2: Gather signals from all sources
        log("\n--- STEP 2: Gather signals ---")
        all_prospects = []

        source_list = sources or ["airtable", "news-db", "sf-leads"]

        # Primary signal source: SQLite news aggregator (replaces Airtable + all RSS calls)
        if "news-db" in source_list:
            log("Source 1: News aggregator (SQLite - 21 RSS feeds)...")
            # Poll feeds first to get fresh articles
            try:
                poll_result = subprocess.run(
                    ["python3", str(Path.home() / ".local/bin/news-aggregator.py")],
                    capture_output=True, text=True, timeout=120,
                    env={**os.environ, "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "")}
                )
                poll_out = poll_result.stdout.strip() if poll_result.stdout else ""
                if poll_out:
                    log(f"  Polled feeds: {poll_out.split(chr(10))[-1]}")
                # Parse feed count from output
                import re as _re
                m = _re.search(r"(\d+) new articles from (\d+)/(\d+) feeds", poll_out)
                if m:
                    new_articles = int(m.group(1))
                    feeds_polled = int(m.group(2))
                    feeds_total = int(m.group(3))
                else:
                    # Fallback: count feeds from stats
                    feeds_polled = poll_out.count("articles") if poll_out else 0
                    feeds_total = 34
            except Exception as e:
                log(f"  Feed poll error: {e}")
            # Now read unprocessed
            try:
                result = subprocess.run(
                    ["python3", str(Path.home() / ".local/bin/news-aggregator.py"),
                     "--unprocessed", "--days", "14"],
                    capture_output=True, text=True, timeout=30,
                )
                news_prospects = json.loads(result.stdout)
                log(f"  News DB: {len(news_prospects)} unprocessed prospects")

                article_ids_seen = set()
                for p in news_prospects:
                    all_prospects.append({
                        "company": p["company"],
                        "domain": p.get("domain"),
                        "persona": None,
                        "source": p.get("source", "news_db"),
                        "signal": p.get("signal", ""),
                        "article_id": p.get("article_id"),
                    })
                    if p.get("article_id"):
                        article_ids_seen.add(p["article_id"])

                # Mark articles as processed
                if article_ids_seen and not DRY_RUN:
                    import sqlite3 as _sqlite3
                    _conn = _sqlite3.connect(os.path.expanduser("~/.openclaw/workspace/data/news.db"))
                    placeholders = ",".join("?" * len(article_ids_seen))
                    _conn.execute(f"UPDATE articles SET outbound_processed = 1 WHERE id IN ({placeholders})", list(article_ids_seen))
                    _conn.commit()
                    _conn.close()
                    log(f"  Marked {len(article_ids_seen)} articles as processed")

            except Exception as e:
                log(f"  News DB error: {e}")

        if "sf-leads" in source_list:
            all_prospects.extend(scan_sf_cold_leads(max_leads=5))


        # Clean up company names using LLM (replaces fragile regex parsing)
        if all_prospects and not DRY_RUN:
            log("Cleaning prospect names via Opus...")
            try:
                sys.path.insert(0, str(Path.home() / '.local/bin'))
                from extract_companies_llm import extract_companies_from_headlines
                
                # Load current customers for exclusion
                customers = load_current_customers()
                customer_names = {c['name'].lower() for c in customers} if customers else set()
                
                headlines = [p.get('signal', p.get('company', '')) for p in all_prospects]
                cleaned = extract_companies_from_headlines(headlines, customer_names)
                
                if cleaned:
                    # Replace the prospect list with LLM-cleaned version
                    cleaned_prospects = []
                    for c in cleaned:
                        # Find the original source info
                        orig = next((p for p in all_prospects if c['headline'] == p.get('signal', '')), {})
                        cleaned_prospects.append({
                            'company': c['company'],
                            'domain': orig.get('domain'),
                            'persona': c.get('persona_guess', orig.get('persona', '')),
                            'source': orig.get('source', 'llm_cleaned'),
                            'signal': c['headline'],
                        })
                    all_prospects = cleaned_prospects
                    log(f"  Cleaned: {len(cleaned_prospects)} real companies from {len(headlines)} headlines")
            except Exception as e:
                log(f"  LLM cleaning error: {e} — using raw prospects")

        # Deduplicate by company name
        seen = set()
        unique_prospects = []
        for p in all_prospects:
            key = p["company"].lower().strip()
            if key not in seen:
                seen.add(key)
                unique_prospects.append(p)

        # Pre-filter: remove known customers, non-companies, and geo-filtered
        pre_filtered = [p for p in unique_prospects if not is_obviously_not_a_company(p["company"])]
        removed = len(unique_prospects) - len(pre_filtered)
        if removed > 0:
            pre_filter_removed = removed
            log(f"  Pre-filter removed {removed} (known customers, non-companies)")
        unique_prospects = pre_filtered

        log(f"\nTotal unique prospects: {len(unique_prospects)}")

        if not unique_prospects:
            log("No new prospects today.")
            notify_telegram(f"Outbound daily scan ({today}): No new prospects found.")
            return

        # Step 2b: Fit filter via Gemini Flash
        log(f"\n--- STEP 2b: Fit filter ({len(unique_prospects)} prospects) ---")
        if not DRY_RUN:
            unique_prospects = fit_filter(unique_prospects)
            if not unique_prospects:
                log("No prospects passed fit filter.")
                notify_telegram(f"Outbound daily scan ({today}): No prospects passed fit filter.")
                return

        # Step 3: Run pipeline on top prospects
        log(f"\n--- STEP 3: Pipeline (top {max_prospects}) ---")
        results = {"ready": 0, "blocked": 0, "no_contact": 0, "errors": 0}

        for i, prospect in enumerate(unique_prospects[:max_prospects]):
            log(f"\n[{i+1}/{min(len(unique_prospects), max_prospects)}] {prospect['company']} ({prospect['source']})")
            log(f"  Signal: {prospect.get('signal', '')[:60]}")

            if DRY_RUN:
                log("  [DRY RUN] Would run pipeline")
                continue

            stdout, rc = run_pipeline_for_prospect(prospect)
            print(stdout)

            if "Sequence ready" in stdout or "READY" in stdout:
                results["ready"] += 1
            elif "BLOCKED" in stdout or "BLOCKED" in stdout.upper():
                results["blocked"] += 1
            elif "NO_CONTACT" in stdout or "No contact" in stdout:
                results["no_contact"] += 1
            else:
                results["errors"] += 1

        # Step 4: Post READY prospects to Telegram
        if not DRY_RUN and results["ready"] > 0:
            log(f"\n--- STEP 4: Post to Telegram ---")
            post_output = subprocess.run(
                ["python3", str(Path.home() / ".local/bin/outbound-approve.py"), "--post-ready"],
                capture_output=True, text=True, timeout=120,
            )
            print(post_output.stdout)

        # Step 5 (sequences) moved to Step 6 at end of pipeline

        # Step 6: Check follow-up sequences (moved here — slow with many contacts)
        if not DRY_RUN:
            log(f"\n--- STEP 6: Check follow-up sequences ---")
            try:
                seq_output = subprocess.run(
                    ["python3", str(Path.home() / ".local/bin/outbound-sequence.py"), "--check", "--send"],
                    capture_output=True, text=True, timeout=600,
                )
                if seq_output.stdout.strip():
                    print(seq_output.stdout[-500:])
            except subprocess.TimeoutExpired:
                log("  Sequence check timed out — will retry next run")
            except Exception as e:
                log(f"  Sequence check error: {e}")

        # Build detailed daily summary
        elapsed = (datetime.now() - start_time).seconds

        # Categorize all processed prospects for the report
        ready_names = []
        blocked_names = []
        no_contact_names = []
        skipped_names = []

        if PROSPECTS_FILE.exists():
            try:
                state_data = json.loads(PROSPECTS_FILE.read_text())
            except OSError:
                # File lock issue (iCloud sync) — retry once
                import time; time.sleep(2)
                state_data = json.loads(PROSPECTS_FILE.read_text())
            for prospect in unique_prospects[:max_prospects]:
                key = prospect["company"].lower().strip()
                p = state_data.get(key, {})
                status = p.get("status", "")
                if status == "READY":
                    contact = (p.get("contact") or {}).get("best_contact", {})
                    ready_names.append(f"{prospect['company']} -> {contact.get('name', '?')} <{contact.get('email', '?')}>")
                elif "BLOCKED" in status:
                    owner = (p.get("roe") or {}).get("owner", "?")
                    ready_names  # just for reference
                    blocked_names.append(f"{prospect['company']} (owner: {owner})")
                elif "NO_CONTACT" in status:
                    no_contact_names.append(prospect["company"])
                elif "SKIPPED" in status:
                    skipped_names.append(prospect["company"])

        summary_lines = [
            f"OUTBOUND DAILY REPORT - {today}",
            f"",
            f"📡 Feeds: {feeds_polled}/{feeds_total} checked | {new_articles} new articles",
            f"🏢 Companies extracted: {len(all_prospects) if 'all_prospects' in dir() else '?'} raw → {len(unique_prospects)} unique",
            f"🚫 Pre-filter removed: {pre_filter_removed} (known customers/non-companies)",
            f"📊 Fit filter: {len(unique_prospects)} evaluated",
            f"🔍 Pipeline: {min(len(unique_prospects), max_prospects)} processed",
            f"",
        ]

        if ready_names:
            summary_lines.append(f"READY TO SEND ({len(ready_names)}):")
            for n in ready_names:
                summary_lines.append(f"  {n}")
            summary_lines.append("")

        if blocked_names:
            summary_lines.append(f"BLOCKED by ROE ({len(blocked_names)}):")
            for n in blocked_names:
                summary_lines.append(f"  {n}")
            summary_lines.append("")

        if no_contact_names:
            summary_lines.append(f"NEED MANUAL HELP - No contact found ({len(no_contact_names)}):")
            for n in no_contact_names:
                summary_lines.append(f"  {n}")
            summary_lines.append("")

        if skipped_names:
            summary_lines.append(f"SKIPPED - Not a fit ({len(skipped_names)}):")
            for n in skipped_names:
                summary_lines.append(f"  {n}")
            summary_lines.append("")

        summary_lines.append(f"Time: {elapsed}s")
        summary = "\n".join(summary_lines)

        log(f"\n{'='*60}")
        log("SUMMARY:")
        log(summary)

        if not DRY_RUN:
            notify_telegram(summary)

        # Save daily log
        log_file = DAILY_LOG_DIR / f"{today}.json"
        log_data = {
            "date": today,
            "sources": source_list,
            "total_scanned": len(unique_prospects),
            "processed": min(len(unique_prospects), max_prospects),
            "results": results,
            "elapsed_seconds": elapsed,
            "prospects": [
                {"company": p["company"], "source": p["source"], "signal": p.get("signal", "")}
                for p in unique_prospects[:max_prospects]
            ],
        }
        log_file.write_text(json.dumps(log_data, indent=2))

    finally:
        if tunnel:
            tunnel.terminate()


def main():
    sources = None
    max_prospects = MAX_PROSPECTS

    if "--source" in sys.argv:
        idx = sys.argv.index("--source")
        source = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        if source:
            sources = [source]

    if "--max" in sys.argv:
        idx = sys.argv.index("--max")
        max_prospects = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else MAX_PROSPECTS

    daily_run(sources=sources, max_prospects=max_prospects)


if __name__ == "__main__":
    main()
