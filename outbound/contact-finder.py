#!/usr/bin/env python3
"""
contact-finder.py — Find the best outbound contact for a company.

Pipeline (email discovery order: Apollo → Hunter → Brave web search → manual):
  1. Check SF for existing Contacts with email at the Account
  2. Apollo mixed_people/search (direct curl) — find by domain + titles
  3. Apollo people/match (enrichment, 1 credit) — get verified email
  4. Hunter Domain-Search + Email-Finder fallback — generate likely email from name + domain
  5. Brave web-search fallback — last-resort email discovery when Apollo + Hunter come up empty
  6. Manual fallback — output what we know, flag for LinkedIn/manual

Email verification (verify_email): Hunter Email-Verifier first; when Hunter errors or is
over quota, fall back to FREE verifiers (disify + mailcheck.ai) instead of failing open.

Usage:
  python3 contact-finder.py "Company Name" "domain.com" [--sf-account-id 001xxx]
  python3 contact-finder.py "Company Name" "domain.com" --json
  python3 contact-finder.py "Company Name" "domain.com" --persona investor

Personas: provider, investor, broker, vendor, user (default: auto-detect)
"""

import json
import subprocess
import sys
import os
import ssl
import urllib.request
import urllib.parse
from pathlib import Path

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

# --- Config ---
APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
APOLLO_BASE = "https://api.apollo.io/api/v1"

HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")  # free tier 1k/mo — last-resort email discovery


def _resolve_state_dir():
    """Prefer openclaw workspace on the remote (iCloud TCC blocks SSH/launchd reads).
    Mirrors the resolver in outbound-daily.py / outbound-pipeline.py / roe-check.py."""
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
            existing = d / "contact-results.json"
            if existing.exists():
                existing.read_text()
            return d
        except (PermissionError, OSError):
            continue
    return Path("/tmp/outbound-v2")


STATE_DIR = _resolve_state_dir()
CONTACT_RESULTS_FILE = STATE_DIR / "contact-results.json"

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

# Title preferences by persona (priority order)
TITLE_PREFS = {
    "provider": [
        "VP Business Development", "VP Sales", "VP Strategy", "Chief Commercial",
        "SVP Growth", "Director Sales", "Director Business Development",
        "VP Marketing", "Chief Revenue",
    ],
    "investor": [
        "Managing Director", "MD Infrastructure", "VP Real Estate",
        "Director Investments", "Partner", "Principal",
        "VP Infrastructure", "Head of Infrastructure", "Investment Director",
        "Senior Vice President",
    ],
    "broker": [
        "Managing Director", "VP Data Center", "Director Data Center",
        "Senior Vice President", "Executive Vice President",
        "VP Advisory", "Director Advisory",
    ],
    "vendor": [
        "VP Sales", "VP Business Development", "Director Sales",
        "VP Market Intelligence", "Regional Sales Director",
        "National Accounts", "Director BD", "VP Strategy",
        "VP Marketing", "Director Marketing", "VP Go to Market",
        "Chief Commercial Officer", "Chief Revenue Officer",
        "VP Market Strategy", "Director Market Strategy",
        "CEO", "President", "Managing Director", "COO",
        "VP Growth", "Director Growth",
    ],
    "user": [
        "VP Infrastructure", "VP Data Center Strategy", "Director Site Selection",
        "VP Real Estate", "Director Procurement", "VP Cloud Infrastructure",
    ],
}

# Generic good titles (used when persona is unknown)
GOOD_TITLES = [
    "VP Business Development", "VP Sales", "VP Strategy",
    "Director Business Development", "Director Sales",
    "Managing Director", "Chief Commercial", "Chief Revenue",
    "VP Real Estate", "VP Infrastructure", "Head of",
    "SVP", "Senior Vice President", "Partner", "Principal",
]

# Titles to avoid
BAD_TITLES = [
    "intern", "assistant", "coordinator", "receptionist",
    "hr", "human resources", "people & adm", "people operations",
    "talent", "recruiting", "recruiter",
    "legal", "counsel", "compliance",
    "accountant", "accounting", "payroll", "controller",
    "chief financial officer", "cfo", "finance director",
    "software engineer", "developer", "sre", "devops",
    "technician", "field service", "maintenance",
    "office manager", "executive assistant",
    "social media", "content manager", "content creator",
    "graphic designer", "web developer", "ux designer",
    "public relations", "communications manager",
]


def log(msg):
    if VERBOSE:
        print(f"  [debug] {msg}", file=sys.stderr)


def sf_query(soql):
    """Run SOQL via sf CLI."""
    result = subprocess.run(
        ["sf", "data", "query", "--query", soql, "--json"],
        capture_output=True, text=True, timeout=30
    )
    try:
        data = json.loads(result.stdout)
        return data.get("result", {}).get("records", [])
    except Exception:
        return []


def apollo_org_search(company_name):
    """Look up an organization's primary domain by company name.

    Used as Step 0 when the prospect arrives without a domain (e.g. news-aggregator
    extracted the company from a headline that didn't include a URL). Without this,
    the rest of the pipeline (Apollo people search, Hunter domain search) can't run.
    Returns the primary_domain string, or None.
    """
    if not company_name:
        return None
    payload = {"q_organization_name": company_name, "page": 1, "per_page": 5}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{APOLLO_BASE}/organizations/search",
        data=data,
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
        # Prefer an org whose name matches the search term (case-insensitive,
        # exact or starts-with). Apollo's relevance ranking can put unrelated
        # tenants first when the name is short or generic.
        target = company_name.strip().lower()
        for o in orgs:
            n = (o.get("name") or "").strip().lower()
            if n == target:
                return o.get("primary_domain")
        for o in orgs:
            n = (o.get("name") or "").strip().lower()
            if n.startswith(target) or target.startswith(n):
                return o.get("primary_domain")
        # Fall back to first result's domain only if the name is very similar.
        first = orgs[0]
        first_name_lc = (first.get("name") or "").strip().lower()
        if target in first_name_lc or first_name_lc in target:
            return first.get("primary_domain")
        return None
    except Exception as e:
        log(f"Apollo org search error: {e}")
        return None


def apollo_search(domain, titles=None, per_page=10):
    """Search Apollo for people at a domain with optional title filter."""
    payload = {
        "q_organization_domains_list": [domain],
        "page": 1,
        "per_page": per_page,
    }
    if titles:
        payload["person_titles"] = titles

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{APOLLO_BASE}/mixed_people/search",
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": APOLLO_API_KEY,
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
            result = json.loads(resp.read())
        return result.get("people", [])
    except Exception as e:
        log(f"Apollo search error: {e}")
        return []


def apollo_enrich(first_name="", last_name="", org_name=None, domain=None, email=None):
    """Enrich a person via Apollo to get verified email + title (1 credit).
    Pass `email` to match directly on a known email address (most reliable)."""
    payload = {
        "first_name": first_name,
        "last_name": last_name,
        "organization_name": org_name,
        "domain": domain,
    }
    if email:
        payload["email"] = email
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{APOLLO_BASE}/people/match",
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": APOLLO_API_KEY,
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
            result = json.loads(resp.read())
        person = result.get("person", {})
        return {
            "name": person.get("name", ""),
            "email": person.get("email"),
            "email_status": person.get("email_status", ""),
            "title": person.get("title", ""),
            "linkedin_url": person.get("linkedin_url", ""),
            "city": person.get("city", ""),
            "state": person.get("state", ""),
            "country": person.get("country", ""),
        }
    except Exception as e:
        log(f"Apollo enrich error: {e}")
        return None


def hunter_domain_search(domain):
    """Use Hunter.io Domain Search to find email pattern and known emails."""
    if not HUNTER_API_KEY:
        return None
    params = urllib.parse.urlencode({
        "domain": domain,
        "api_key": HUNTER_API_KEY,
        "limit": 10,
    })
    try:
        req = urllib.request.Request(f"https://api.hunter.io/v2/domain-search?{params}")
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as resp:
            result = json.loads(resp.read())
        data = result.get("data", {})
        return {
            "pattern": data.get("pattern", ""),
            "organization": data.get("organization", ""),
            "emails": [
                {
                    "email": e.get("value", ""),
                    "first_name": e.get("first_name", ""),
                    "last_name": e.get("last_name", ""),
                    "position": e.get("position", ""),
                    "confidence": e.get("confidence", 0),
                }
                for e in data.get("emails", [])
            ],
        }
    except Exception as e:
        log(f"Hunter domain search error: {e}")
        return None


def hunter_find_email(first_name, last_name, domain):
    """Use Hunter.io Email-Finder to generate a likely email."""
    if not HUNTER_API_KEY:
        return None
    params = urllib.parse.urlencode({
        "domain": domain,
        "first_name": first_name,
        "last_name": last_name,
        "api_key": HUNTER_API_KEY,
    })
    try:
        req = urllib.request.Request(f"https://api.hunter.io/v2/email-finder?{params}")
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as resp:
            result = json.loads(resp.read())
        data = result.get("data", {})
        return {
            "email": data.get("email"),
            "score": data.get("score", 0),
            "status": data.get("verification", {}).get("status", ""),
        }
    except Exception as e:
        log(f"Hunter error: {e}")
        return None


def _free_verify(email):
    """Free email-verification fallback (disify + mailcheck.ai) for when Hunter is
    unavailable or over quota. These check syntax, MX/DNS, and disposable-domain status —
    NOT true mailbox existence — so they fail OPEN on inconclusive signals but CLOSED on
    clearly-bad ones (disposable domain or no MX). This replaces the old fail-open behavior
    where a Hunter error silently passed unverified emails. See the .bak-free-verifier
    backups — this capability existed before and was lost to script drift."""
    if not email or "@" not in email:
        return False, "free_verify: bad email"
    verdicts = []
    disposable = False
    mx_ok = None  # None = unknown, True/False = determined
    _UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    # disify — {"format":bool,"dns":bool,"disposable":bool,"domain":str}
    try:
        req = urllib.request.Request(f"https://www.disify.com/api/email/{urllib.parse.quote(email)}", headers=_UA)
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as resp:
            d = json.loads(resp.read())
        verdicts.append(f"disify(format={d.get('format')},dns={d.get('dns')},disp={d.get('disposable')})")
        if d.get("format") is False:
            return False, "free_verify: " + "; ".join(verdicts) + " — bad format"
        if d.get("disposable"):
            disposable = True
        if d.get("dns") is not None:
            mx_ok = bool(d.get("dns"))
    except Exception as e:
        verdicts.append(f"disify(err:{e})")
    # mailcheck.ai — {"mx":bool,"disposable":bool,...}
    try:
        req = urllib.request.Request(f"https://api.mailcheck.ai/email/{urllib.parse.quote(email)}", headers=_UA)
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as resp:
            m = json.loads(resp.read())
        verdicts.append(f"mailcheck(mx={m.get('mx')},disp={m.get('disposable')})")
        if m.get("disposable"):
            disposable = True
        if m.get("mx") is not None:
            mx_ok = bool(m.get("mx")) if mx_ok is None else (mx_ok and bool(m.get("mx")))
    except Exception as e:
        verdicts.append(f"mailcheck(err:{e})")
    detail = "free_verify: " + "; ".join(verdicts)
    log(detail)
    if disposable:
        return False, detail + " — disposable"
    if mx_ok is False:
        return False, detail + " — no MX/DNS"
    if all("err:" in v for v in verdicts):
        # both free verifiers unreachable — last-resort proceed (mirrors prior fail-open)
        return True, detail + " — both verifiers unreachable, proceeding"
    return True, detail + " — passed (MX ok, not disposable)"


# Free/personal mail domains — excluded when picking a company's CORPORATE email domain
# (still allowed as a direct hit for a specific person if clearly theirs).
_FREEMAIL = {
    "gmail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com", "live.com",
    "msn.com", "aol.com", "icloud.com", "me.com", "proton.me", "protonmail.com", "gmx.com",
}


def _brave_snippets(query, count=10):
    """Run one Brave web search; return result snippet strings (title+desc+url). [] on failure."""
    if not BRAVE_API_KEY:
        return []
    try:
        params = urllib.parse.urlencode({"q": query, "count": count})
        req = urllib.request.Request(
            f"https://api.search.brave.com/res/v1/web/search?{params}",
            headers={"X-Subscription-Token": BRAVE_API_KEY, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=12, context=SSL_CTX) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log(f"Brave search error: {e}")
        return []
    return [" ".join(str(r.get(k, "")) for k in ("title", "description", "url"))
            for r in data.get("web", {}).get("results", [])]


def _harvest_emails(snippets):
    """Extract (local, domain, snippet) for every email address found in the snippets."""
    import re as _re
    rx = _re.compile(r"([A-Za-z0-9._%+\-]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
    out = []
    for s in snippets:
        for m in rx.finditer(s):
            out.append((m.group(1).lower(), m.group(2).lower(), s))
    return out


def brave_find_email(name, company, domain=None):
    """Step 5.0: find the TARGET person's real email ANYWHERE on the web — not restricted to
    `domain` (a verified address on the internet is enough). Ranks found addresses: prefers
    corporate (non-freemail), the person's name in the local-part, a name-derived pattern, and
    the passed domain. Returns the best address or None if nothing credibly tied to the person."""
    if not BRAVE_API_KEY or not name:
        return None
    parts = name.split()
    first = parts[0].lower() if parts else ""
    last = parts[-1].lower() if len(parts) > 1 else ""
    dom = domain.lower() if domain else None
    snippets = []
    for q in [f'"{name}" "{company}" email',
              f'"{name}" {company} contact email',
              (f'"{name}" {domain}' if domain else f'"{name}" "{company}"')]:
        snippets += _brave_snippets(q)
    emails = _harvest_emails(snippets)
    if not emails:
        return None

    def score(local, edom):
        sc = 0
        if edom not in _FREEMAIL:
            sc += 3
        if dom and edom == dom:
            sc += 2
        if first and first in local:
            sc += 2
        if last and last in local:
            sc += 2
        if first and last and _derive_pattern(local, first, last):
            sc += 3
        return sc

    best = max(emails, key=lambda e: score(e[0], e[1]))
    if score(best[0], best[1]) <= 0:  # no credible tie to the person/company
        return None
    return f"{best[0]}@{best[1]}"


# Email-pattern templates, ordered most→least common in corporate use. Tokens:
# {first} {last} = full names lowercased, {f} {l} = first/last initials.
_EMAIL_PATTERNS = [
    "{first}.{last}", "{first}{last}", "{f}{last}", "{first}{l}", "{f}.{last}",
    "{first}.{l}", "{first}_{last}", "{last}.{first}", "{last}{first}", "{f}{l}",
    "{last}{f}", "{first}", "{last}",
]


def _derive_pattern(local, first, last):
    """Given an email local-part and the owner's first/last name, return the pattern
    template it matches (e.g. '{first}.{last}'), or None if no arrangement fits."""
    first, last = (first or "").lower(), (last or "").lower()
    if not first or not last:
        return None
    f, l = first[0], last[0]
    for t in _EMAIL_PATTERNS:
        if t.format(first=first, last=last, f=f, l=l) == local.lower():
            return t
    return None


def _apply_pattern(template, first, last):
    """Build a local-part from a pattern template + a target's first/last name."""
    first, last = (first or "").lower(), (last or "").lower()
    if not first or not last:
        return None
    try:
        return template.format(first=first, last=last, f=first[0], l=last[0])
    except Exception:
        return None


def brave_infer_pattern(company, domain=None):
    """Step 5.2: discover the company's email STRUCTURE from ANY verified address on the
    internet — NOT limited to `domain`, since a firm's mail domain often differs from its
    website (e.g. hed.co vs hedev.com). Returns (email_domain, pattern_template) or None.
    Picks the dominant CORPORATE (non-freemail) domain found, preferring the passed website
    domain only if real addresses there actually exist, then derives the pattern from
    addresses whose owner name appears in the same snippet."""
    if not BRAVE_API_KEY:
        return None
    import re as _re
    from collections import Counter
    name_re = _re.compile(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b")
    snippets = []
    for q in [f'"{company}" email', f'{company} contact email',
              f'{company} email address format',
              (f'"@{domain}"' if domain else f'{company} team email')]:
        snippets += _brave_snippets(q)
    emails = _harvest_emails(snippets)  # (local, dom, snippet)
    if not emails:
        return None

    # Choose the corporate email domain: prefer the website domain only if real addresses
    # there were actually found; otherwise the most common non-freemail domain on the web.
    corp = Counter(d for _, d, _ in emails if d not in _FREEMAIL)
    if not corp:
        return None
    dom = domain.lower() if domain else None
    email_domain = dom if (dom and dom in corp) else corp.most_common(1)[0][0]

    # Derive the pattern from name-matched samples at the chosen domain.
    votes = Counter()
    for local, edom, snip in emails:
        if edom != email_domain:
            continue
        matched = False
        for nm in name_re.finditer(snip):
            t = _derive_pattern(local, nm.group(1), nm.group(2))
            if t:
                votes[t] += 2  # name-confirmed = strong vote
                matched = True
                break
        if not matched and "." in local:
            votes["{first}.{last}"] += 1  # weak structural guess
    if not votes:
        return None
    pattern = votes.most_common(1)[0][0]
    log(f"Brave inferred email domain '{email_domain}' + pattern '{pattern}' for {company}")
    return (email_domain, pattern)


def verify_email(email):
    """
    Verify an email via Hunter.io Email-Verifier, falling back to free verifiers
    (disify + mailcheck.ai) when Hunter errors or is over quota — never fail open silently.
    Returns (is_valid, detail_str) — is_valid=True means OK to use.
    """
    if not email:
        return True, "skipped (no email)"
    if not HUNTER_API_KEY:
        return _free_verify(email)
    params = urllib.parse.urlencode({
        "email": email,
        "api_key": HUNTER_API_KEY,
    })
    try:
        req = urllib.request.Request(f"https://api.hunter.io/v2/email-verifier?{params}")
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
            result = json.loads(resp.read())
        if "data" not in result or result.get("errors"):
            log("Hunter verify: no data / over quota — falling back to free verifiers")
            ok, fdetail = _free_verify(email)
            return ok, f"hunter_noquota→{fdetail}"
        data = result.get("data", {})
        status = data.get("status", "unknown")
        score = data.get("score", 0)
        detail = f"hunter_verify: status={status}, score={score}"
        log(detail)

        if status in ("valid", "accept_all"):
            return True, detail
        if status in ("invalid", "disposable"):
            return False, detail
        # status == "unknown" or "webmail"
        if status == "unknown" and score >= 50:
            return True, f"{detail} (proceed with caution)"
        if status == "unknown" and score < 50:
            return False, detail
        # webmail — allow it (personal email but real)
        if status == "webmail":
            return True, detail
        return True, detail
    except Exception as e:
        log(f"Hunter verify error ({e}) — falling back to free verifiers (disify + mailcheck.ai)")
        ok, fdetail = _free_verify(email)
        return ok, f"hunter_error→{fdetail}"


def score_title(title, persona=None):
    """Score a title for outbound fit. Higher = better."""
    if not title:
        return 0
    title_lower = title.lower()

    # Disqualifiers
    for bad in BAD_TITLES:
        if bad in title_lower:
            return -10

    # Persona-specific scoring
    if persona and persona in TITLE_PREFS:
        prefs = TITLE_PREFS[persona]
        for i, pref in enumerate(prefs):
            if pref.lower() in title_lower:
                return 100 - i  # Higher rank = higher score

    # Generic scoring
    score = 0

    # Seniority signals
    if any(x in title_lower for x in ["ceo", "coo", "cro", "cco"]):
        score += 70
    if "president" in title_lower and "vice" not in title_lower:
        score += 75
    if "chief" in title_lower and any(x in title_lower for x in ["commercial", "revenue", "growth", "strategy", "investment"]):
        score += 85
    if "chief" in title_lower and any(x in title_lower for x in ["financial", "technology", "operating", "executive"]):
        score += 65
    if any(x in title_lower for x in ["founder", "co-founder", "cofounder", "owner"]):
        score += 68
    if any(x in title_lower for x in ["svp", "senior vice president", "evp", "executive vice president"]):
        score += 85
    if ("vice president" in title_lower or "vp " in title_lower or title_lower.startswith("vp")) and "account" not in title_lower:
        score += 80
    # "Account Executive" is a sales rep, not a senior exec
    if "account executive" in title_lower or "account manager" in title_lower:
        score = max(score, 25)  # Cap low
    if "managing director" in title_lower or title_lower == "md":
        score += 80
    if "partner" in title_lower or "principal" in title_lower:
        score += 75
    if "director" in title_lower:
        score += 60
    if "head of" in title_lower:
        score += 70
    if "manager" in title_lower:
        score += 40
    if "analyst" in title_lower:
        score += 20
    if "associate" in title_lower:
        score += 15

    # Function signals (bonus)
    if any(x in title_lower for x in ["business development", "bd", "sales", "growth", "commercial", "revenue"]):
        score += 15
    if any(x in title_lower for x in ["strategy", "strategic", "investment", "infrastructure"]):
        score += 10
    if any(x in title_lower for x in ["real estate", "site selection", "data center"]):
        score += 10
    if any(x in title_lower for x in ["marketing", "go to market", "market strategy", "gtm"]):
        score += 12

    return score


def find_sf_contacts(account_id):
    """Check SF for existing contacts at the account."""
    records = sf_query(
        f"SELECT Id, Name, FirstName, LastName, Email, Title, Phone "
        f"FROM Contact WHERE AccountId = '{account_id}' "
        f"AND Email != null "
        f"ORDER BY CreatedDate DESC LIMIT 20"
    )
    contacts = []
    for r in records:
        contacts.append({
            "source": "salesforce",
            "name": r.get("Name", ""),
            "first_name": r.get("FirstName", ""),
            "last_name": r.get("LastName", ""),
            "email": r.get("Email", ""),
            "title": r.get("Title", ""),
            "phone": r.get("Phone", ""),
            "sf_id": r.get("Id", ""),
        })
    return contacts


def find_contact(company_name, domain, sf_account_id=None, persona=None):
    """
    Full contact finding pipeline.
    Returns dict with best contact and all candidates.
    """
    result = {
        "company": company_name,
        "domain": domain,
        "persona": persona,
        "best_contact": None,
        "candidates": [],
        "pipeline_steps": [],
        "email_source": None,
    }

    # Step 0: Resolve domain from company name when none was provided.
    # News-aggregator-sourced prospects often arrive with domain=None because the
    # source headline didn't contain a URL. Without this step, the entire Apollo +
    # Hunter pipeline below short-circuits and the prospect gets surfaced as
    # "manual help needed" — even when the company has a perfectly findable domain.
    if not domain:
        log(f"Step 0: No domain provided — looking up via Apollo org search for '{company_name}'")
        inferred = apollo_org_search(company_name)
        if inferred:
            log(f"  Inferred domain: {inferred}")
            domain = inferred
            result["domain"] = inferred
            result["pipeline_steps"].append(f"Apollo org search inferred domain: {inferred}")
        else:
            result["pipeline_steps"].append("Apollo org search: no domain found for company name")

    # Step 1: Check SF for existing contacts
    if sf_account_id:
        log(f"Step 1: Checking SF contacts for account {sf_account_id}")
        sf_contacts = find_sf_contacts(sf_account_id)
        result["pipeline_steps"].append(f"SF contacts: {len(sf_contacts)} found with email")

        for c in sf_contacts:
            c["title_score"] = score_title(c.get("title", ""), persona)
            result["candidates"].append(c)

        # If we have a good SF contact, use it
        good_sf = [c for c in sf_contacts if c["title_score"] > 50]
        if good_sf:
            best = max(good_sf, key=lambda x: x["title_score"])
            result["best_contact"] = best
            result["email_source"] = "salesforce"
            log(f"  Found good SF contact: {best['name']} ({best['title']})")
            return result

    # Step 2: Apollo search
    if domain:
        log(f"Step 2: Apollo search for {domain}")
        # Search with title preferences
        search_titles = GOOD_TITLES[:5]
        if persona and persona in TITLE_PREFS:
            search_titles = TITLE_PREFS[persona][:5]

        people = apollo_search(domain, titles=search_titles)

        if not people:
            # Broaden search — no title filter
            log("  No results with title filter, broadening...")
            people = apollo_search(domain)

        result["pipeline_steps"].append(f"Apollo search: {len(people)} people found")

        apollo_candidates = []
        for p in people:
            title = p.get("title", "")
            ts = score_title(title, persona)
            if ts < 0:
                continue  # Skip bad titles
            apollo_candidates.append({
                "source": "apollo",
                "name": p.get("name", ""),
                "first_name": p.get("first_name", ""),
                "last_name": p.get("last_name", ""),
                "title": title,
                "title_score": ts,
                "linkedin_url": p.get("linkedin_url", ""),
                "city": p.get("city", ""),
                "country": p.get("country", ""),
                "email_status": p.get("email_status", ""),
            })

        # Sort by title score
        apollo_candidates.sort(key=lambda x: x["title_score"], reverse=True)
        result["candidates"].extend(apollo_candidates)

        # Step 3: Enrich best Apollo candidate
        if apollo_candidates:
            best_candidate = apollo_candidates[0]
            log(f"Step 3: Enriching {best_candidate['name']} ({best_candidate['title']})")

            enriched = apollo_enrich(
                best_candidate.get("first_name", ""),
                best_candidate.get("last_name", ""),
                company_name,
                domain,
            )

            if enriched and enriched.get("email"):
                email_status = enriched.get("email_status", "")
                enriched_email = enriched["email"]
                enriched_domain = enriched_email.split("@")[1] if "@" in enriched_email else ""

                # DOMAIN VALIDATION: reject if enriched email domain is clearly different
                # Allow: subdomain matches, short-form matches (denvr.com for denvrdata.com)
                def domains_match(d1, d2):
                    if not d1 or not d2:
                        return True
                    d1, d2 = d1.lower(), d2.lower()
                    if d1 == d2:
                        return True
                    # One contains the other (denvr.com / denvrdata.com share "denvr")
                    base1 = d1.split(".")[0]
                    base2 = d2.split(".")[0]
                    if base1 in base2 or base2 in base1:
                        return True
                    # Subdomain match
                    if d1.endswith("." + d2) or d2.endswith("." + d1):
                        return True
                    return False

                if domain and enriched_domain and not domains_match(enriched_domain, domain):
                    log(f"  Domain mismatch: enriched={enriched_domain}, target={domain} — skipping")
                    result["pipeline_steps"].append(
                        f"Apollo enrich: {enriched_email} REJECTED (domain mismatch: {enriched_domain} != {domain})"
                    )
                else:
                    result["pipeline_steps"].append(
                        f"Apollo enrich: {enriched_email} (status: {email_status})"
                    )

                    if email_status in ("verified", "guessed"):
                        # Verify email before accepting
                        email_ok, verify_detail = verify_email(enriched_email)
                        result["pipeline_steps"].append(verify_detail)
                        if email_ok:
                            best_candidate["email"] = enriched_email
                            best_candidate["email_status"] = email_status
                            best_candidate["linkedin_url"] = enriched.get("linkedin_url", best_candidate.get("linkedin_url", ""))
                            result["best_contact"] = best_candidate
                            result["email_source"] = f"apollo_enrich ({email_status})"
                            return result
                        else:
                            log(f"  Email {enriched_email} failed Hunter verification — trying next")
                    else:
                        best_candidate["email"] = enriched_email
                        best_candidate["email_status"] = email_status
                        log(f"  Email found but status is '{email_status}' — trying fallbacks")
            else:
                result["pipeline_steps"].append("Apollo enrich: no email returned")
                log("  Apollo enrich returned no email")

            # Try second-best candidate if first failed
            if len(apollo_candidates) > 1 and not result["best_contact"]:
                second = apollo_candidates[1]
                log(f"Step 3b: Trying second candidate: {second['name']} ({second['title']})")
                enriched2 = apollo_enrich(
                    second.get("first_name", ""),
                    second.get("last_name", ""),
                    company_name,
                    domain,
                )
                if enriched2 and enriched2.get("email"):
                    email_status = enriched2.get("email_status", "")
                    enriched2_email = enriched2["email"]
                    enriched2_domain = enriched2_email.split("@")[1] if "@" in enriched2_email else ""

                    if domain and enriched2_domain and enriched2_domain != domain:
                        log(f"  Domain mismatch on 2nd: {enriched2_domain} != {domain}")
                        result["pipeline_steps"].append(
                            f"Apollo enrich #2: {enriched2_email} REJECTED (domain mismatch)"
                        )
                    else:
                        result["pipeline_steps"].append(
                            f"Apollo enrich #2: {enriched2_email} (status: {email_status})"
                        )
                        if email_status in ("verified", "guessed"):
                            email_ok, verify_detail = verify_email(enriched2_email)
                            result["pipeline_steps"].append(verify_detail)
                            if email_ok:
                                second["email"] = enriched2_email
                                second["email_status"] = email_status
                                second["linkedin_url"] = enriched2.get("linkedin_url", second.get("linkedin_url", ""))
                                result["best_contact"] = second
                                result["email_source"] = f"apollo_enrich_2nd ({email_status})"
                                return result
                            else:
                                log(f"  Email {enriched2_email} failed Hunter verification — skipping")

            # Step 4: Hunter Domain Search + Email Finder
            if not result["best_contact"] and HUNTER_API_KEY:
                # 4a: Domain Search — discover email pattern and known contacts
                log(f"Step 4a: Hunter Domain Search for {domain}")
                domain_result = hunter_domain_search(domain)
                if domain_result:
                    pattern = domain_result.get("pattern", "")
                    known_emails = domain_result.get("emails", [])
                    result["pipeline_steps"].append(
                        f"Hunter domain: pattern='{pattern}', {len(known_emails)} known emails"
                    )

                    # Check if any known Hunter contacts match our title criteria
                    for he in known_emails:
                        ts = score_title(he.get("position", ""), persona)
                        if ts > 50 and he.get("email"):
                            email_ok, verify_detail = verify_email(he["email"])
                            result["pipeline_steps"].append(verify_detail)
                            if not email_ok:
                                log(f"  Hunter contact {he['email']} failed verification — trying next")
                                continue
                            result["pipeline_steps"].append(
                                f"Hunter found: {he['email']} ({he.get('position', '?')})"
                            )
                            hunter_contact = {
                                "source": "hunter_domain",
                                "name": f"{he.get('first_name', '')} {he.get('last_name', '')}".strip(),
                                "first_name": he.get("first_name", ""),
                                "last_name": he.get("last_name", ""),
                                "email": he["email"],
                                "title": he.get("position", ""),
                                "title_score": ts,
                                "email_status": f"hunter_confidence_{he.get('confidence', '?')}",
                            }
                            result["best_contact"] = hunter_contact
                            result["email_source"] = "hunter_domain_search"
                            result["candidates"].append(hunter_contact)
                            return result

                # 4b: Email Finder — generate email for best Apollo candidate
                target = apollo_candidates[0]
                log(f"Step 4b: Hunter Email-Finder for {target['first_name']} {target['last_name']} @ {domain}")
                hunter_result = hunter_find_email(
                    target.get("first_name", ""),
                    target.get("last_name", ""),
                    domain,
                )
                if hunter_result and hunter_result.get("email"):
                    result["pipeline_steps"].append(
                        f"Hunter finder: {hunter_result['email']} (score: {hunter_result.get('score', '?')})"
                    )
                    email_ok, verify_detail = verify_email(hunter_result["email"])
                    result["pipeline_steps"].append(verify_detail)
                    if email_ok:
                        target["email"] = hunter_result["email"]
                        target["email_status"] = f"hunter_score_{hunter_result.get('score', '?')}"
                        result["best_contact"] = target
                        result["email_source"] = "hunter_email_finder"
                        return result
                    else:
                        log(f"  Hunter-found email {hunter_result['email']} failed verification")
                else:
                    result["pipeline_steps"].append("Hunter finder: no email found")

            # Step 5: Manual fallback — return best candidate without email
            if not result["best_contact"] and apollo_candidates:
                best = apollo_candidates[0]
                best["email"] = None
                best["email_status"] = "not_found"
                result["best_contact"] = best
                result["email_source"] = "manual_needed"
                result["pipeline_steps"].append(
                    f"Manual fallback: {best['name']} — needs LinkedIn/manual email discovery"
                )

    # If Apollo returned nothing, try Hunter Domain Search as standalone
    if not result["best_contact"] and domain and HUNTER_API_KEY and not any("Hunter" in s for s in result["pipeline_steps"]):
        log(f"Fallback: Hunter Domain Search (Apollo returned nothing)")
        domain_result = hunter_domain_search(domain)
        if domain_result and domain_result.get("emails"):
            known = domain_result["emails"]
            result["pipeline_steps"].append(f"Hunter domain fallback: {len(known)} emails found")
            # Pick best by title score
            scored = []
            for he in known:
                ts = score_title(he.get("position", ""), persona)
                if ts >= 0 and he.get("email"):
                    scored.append((ts, he))
            scored.sort(key=lambda x: x[0], reverse=True)
            for ts, best_he in scored:
                if not best_he.get("email"):
                    continue
                email_ok, verify_detail = verify_email(best_he["email"])
                result["pipeline_steps"].append(verify_detail)
                if not email_ok:
                    log(f"  Fallback Hunter contact {best_he['email']} failed verification — trying next")
                    continue
                contact = {
                    "source": "hunter_domain",
                    "name": f"{best_he.get('first_name', '')} {best_he.get('last_name', '')}".strip(),
                    "first_name": best_he.get("first_name", ""),
                    "last_name": best_he.get("last_name", ""),
                    "email": best_he["email"],
                    "title": best_he.get("position", ""),
                    "title_score": ts,
                    "email_status": f"hunter_confidence_{best_he.get('confidence', '?')}",
                }
                result["best_contact"] = contact
                result["email_source"] = "hunter_domain_search"
                result["candidates"].append(contact)
                return result

    # Step 5: Brave web-search fallback — last resort when Apollo + Hunter produced a name
    # but no usable email (best_contact may already be set with email=None by the manual
    # fallback). Order: Apollo → Hunter → Brave. Two sub-steps:
    #   5.0 — find the TARGET contact's own email published on the web
    #   5.2 — if none, infer the company's public email STRUCTURE from any address found at
    #         the domain (even a wrong person's), then construct the target's email from it
    _bc = result.get("best_contact") or {}
    _cand = _bc if _bc.get("name") else ((result.get("candidates") or [None])[0])
    if _cand and not _cand.get("email") and _cand.get("name") and domain and BRAVE_API_KEY:
        _cf = _cand.get("first_name") or (_cand["name"].split()[0] if _cand.get("name") else "")
        _cl = _cand.get("last_name") or (_cand["name"].split()[-1] if _cand.get("name") else "")
        found, source = None, None

        # 5.0 — direct: is the target's own email published anywhere?
        log(f"Step 5.0: Brave direct email search for {_cand['name']} @ {domain}")
        direct = brave_find_email(_cand.get("name", ""), company_name, domain)
        if direct:
            found, source = direct, "brave_web_search"
            result["pipeline_steps"].append(f"Brave 5.0 direct: found {found}")

        # 5.2 — pattern: confirm the org's email structure from ANY verified address on the
        # web (its real mail domain may differ from the website), then build the target's address
        if not found and _cf and _cl:
            log(f"Step 5.2: Brave email-pattern inference for {company_name}")
            res = brave_infer_pattern(company_name, domain)
            if res:
                edomain, pattern = res
                local = _apply_pattern(pattern, _cf, _cl)
                if local:
                    found = f"{local}@{edomain}"
                    source = f"brave_pattern ({pattern}@{edomain})"
                    result["pipeline_steps"].append(
                        f"Brave 5.2 pattern '{pattern}' @ {edomain} → constructed {found}"
                    )

        if found:
            email_ok, verify_detail = verify_email(found)
            result["pipeline_steps"].append(verify_detail)
            if email_ok:
                _cand["email"] = found
                _cand["email_status"] = source
                result["best_contact"] = _cand
                result["email_source"] = source
                if _cand not in result.get("candidates", []):
                    result.setdefault("candidates", []).append(_cand)
                return result
            else:
                log(f"  {source} email {found} failed verification — leaving for manual")
        else:
            result["pipeline_steps"].append("Brave 5.0/5.2: no email or pattern found")

    # No domain provided and no SF contacts
    if not result["best_contact"]:
        result["pipeline_steps"].append("No contacts found — need domain or SF account ID")
        result["email_source"] = "none"

    return result


def print_result(result):
    """Pretty-print contact finder result."""
    best = result.get("best_contact")
    source = result.get("email_source", "none")

    if best and best.get("email"):
        icon = "✅" if "verified" in str(best.get("email_status", "")) else "⚠️"
        print(f"\n{icon}  {result['company']} — Contact Found ({source})")
        print(f"   Name:     {best.get('name', '?')}")
        print(f"   Title:    {best.get('title', '?')}")
        print(f"   Email:    {best.get('email', '?')}")
        print(f"   Status:   {best.get('email_status', '?')}")
        if best.get("linkedin_url"):
            print(f"   LinkedIn: {best['linkedin_url']}")
        if best.get("city") or best.get("country"):
            loc = ", ".join(filter(None, [best.get("city", ""), best.get("state", ""), best.get("country", "")]))
            print(f"   Location: {loc}")
    elif best:
        print(f"\n⚠️  {result['company']} — Contact Found, No Email")
        print(f"   Name:     {best.get('name', '?')}")
        print(f"   Title:    {best.get('title', '?')}")
        if best.get("linkedin_url"):
            print(f"   LinkedIn: {best['linkedin_url']}")
        print(f"   Action:   Manual email discovery needed")
    else:
        print(f"\n❌  {result['company']} — No Contact Found")

    print(f"\n   Pipeline:")
    for step in result.get("pipeline_steps", []):
        print(f"     → {step}")

    if len(result.get("candidates", [])) > 1:
        print(f"\n   Other Candidates ({len(result['candidates']) - 1}):")
        for c in result["candidates"][1:6]:  # Show top 5 alternates
            email_info = f" <{c['email']}>" if c.get("email") else ""
            print(f"     • {c.get('name', '?')} — {c.get('title', '?')}{email_info} (score: {c.get('title_score', 0)})")

    print()


def save_result(result):
    """Save result to state file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {}
    if CONTACT_RESULTS_FILE.exists():
        state = json.loads(CONTACT_RESULTS_FILE.read_text())

    key = result["company"].lower().strip()
    state[key] = result
    CONTACT_RESULTS_FILE.write_text(json.dumps(state, indent=2, default=str))


def main():
    # Build positional args while skipping flag values. The previous one-liner kept
    # the value that followed each flag — e.g. `--persona vendor` resulted in
    # args=['Company','vendor'] and `vendor` was treated as the domain. That made
    # contact-finder run Apollo + Hunter against a junk "vendor" domain instead of
    # the real one (mirrors the 2026-04-28 outbound-pipeline argparse bug).
    FLAGS_WITH_VALUE = {"--persona", "--sf-account-id", "--domain", "--batch"}
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

    if len(args) < 1:
        print("Usage: contact-finder.py 'Company Name' 'domain.com' [--sf-account-id 001xxx] [--persona investor]")
        print("\nPersonas: provider, investor, broker, vendor, user")
        print("Flags: -v/--verbose, --json")
        sys.exit(1)

    company = args[0]
    domain = args[1] if len(args) > 1 else None

    # Parse optional flags
    sf_account_id = None
    persona = None
    if "--sf-account-id" in sys.argv:
        idx = sys.argv.index("--sf-account-id")
        sf_account_id = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
    if "--persona" in sys.argv:
        idx = sys.argv.index("--persona")
        persona = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None

    # If no SF account ID provided but we have a domain, try to find it
    if not sf_account_id and domain:
        domain_clean = domain.replace("https://", "").replace("http://", "").replace("www.", "")
        records = sf_query(
            f"SELECT Id FROM Account WHERE Website LIKE '%{domain_clean}%' LIMIT 1"
        )
        if records:
            sf_account_id = records[0]["Id"]
            log(f"Found SF account by domain: {sf_account_id}")
        else:
            # Try by name
            name_clean = company.replace("'", "\\'")
            records = sf_query(
                f"SELECT Id FROM Account WHERE Name LIKE '%{name_clean}%' LIMIT 1"
            )
            if records:
                sf_account_id = records[0]["Id"]
                log(f"Found SF account by name: {sf_account_id}")

    result = find_contact(company, domain, sf_account_id, persona)

    if "--json" in sys.argv:
        print(json.dumps(result, indent=2, default=str))
    else:
        print_result(result)

    save_result(result)


if __name__ == "__main__":
    main()
