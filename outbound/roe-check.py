#!/usr/bin/env python3
"""
roe-check.py — Rules of Engagement checker for datacenterHawk outbound.

Checks whether a company is CLEAR or BLOCKED for outbound prospecting
based on Salesforce activity: engagement window 90d, outreach in-flight 30d, new-contact window 120d.

Usage:
  python3 roe-check.py "Company Name" "domain.com"
  python3 roe-check.py "Company Name"                   # domain optional
  python3 roe-check.py --batch /path/to/companies.csv   # batch mode
  python3 roe-check.py --recheck                        # re-run all CLEAR from state

Output: JSON with status, details, and reasoning.

Uses sf CLI (this Mac) or REST API (remote machine, via --rest flag).
"""

import json
import subprocess
import sys
import re
import ssl
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# --- Config ---
LOOKBACK_DAYS = 120        # widest activity-gather window; also the "outbounded in last 120d -> use a NEW contact" window
ENGAGE_DAYS = 90           # another rep's engagement (response/call/meeting) within this => ACTIVE, blocked (ROE updated 2026-07, was 120)
OPP_REVISIT_DAYS = 180


def _resolve_state_dir():
    """Prefer iCloud for local Mac; fall back to openclaw workspace for remote/cron.
    macOS TCC blocks iCloud access from SSH/cron sessions — test read+write of an
    existing file, not just new-file writes (TCC sometimes allows new writes but
    blocks reads of synced files)."""
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
            # Also try reading any existing roe-results.json — TCC can block
            # reads of pre-synced files even when new writes succeed.
            existing = d / "roe-results.json"
            if existing.exists():
                existing.read_text()
            return d
        except (PermissionError, OSError):
            continue
    return Path("/tmp/outbound-v2")


STATE_DIR = _resolve_state_dir()
STATE_FILE = STATE_DIR / "roe-results.json"

USE_REST = "--rest" in sys.argv
VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv


def log(msg):
    if VERBOSE:
        print(f"  [debug] {msg}", file=sys.stderr)


def sf_query(soql):
    """Run SOQL via sf CLI and return records."""
    log(f"SOQL: {soql[:120]}...")
    result = subprocess.run(
        ["sf", "data", "query", "--query", soql, "--json", "-o", "you@example.com"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        # Some queries return 0 records with a non-zero exit — check stderr
        log(f"sf CLI stderr: {result.stderr[:200]}")
        # Try to parse anyway
        try:
            data = json.loads(result.stdout)
            return data.get("result", {}).get("records", [])
        except Exception:
            return []

    data = json.loads(result.stdout)
    records = data.get("result", {}).get("records", [])
    log(f"  → {len(records)} records")
    return records


def sf_count(soql):
    """Run a COUNT() SOQL query and return the integer."""
    records = sf_query(soql)
    if records:
        # COUNT() returns {"expr0": N}
        for key, val in records[0].items():
            if key.startswith("expr") or key == "cnt":
                return int(val)
            if isinstance(val, int):
                return val
    return 0


def _load_parent_domain_map():
    """Load subsidiary->parent domain map from vault (or remote-machine fallback paths)."""
    for p in [
        Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/hank-vault/config/parent-domain-map.json",
        Path.home() / ".openclaw/config/parent-domain-map.json",
        Path.home() / ".local/share/outbound/parent-domain-map.json",
    ]:
        if p.exists():
            try:
                return json.loads(p.read_text()).get("map", {}) or {}
            except Exception:
                pass
    return {}

PARENT_DOMAIN_MAP = _load_parent_domain_map()


def _normalize_domain(value):
    """Strip protocol/www/path from a website-or-domain string and lowercase it."""
    if not value:
        return ""
    return (
        value.lower()
        .replace("https://", "")
        .replace("http://", "")
        .replace("www.", "")
        .split("/", 1)[0]
        .strip()
    )


# --- Manual ROE carve-outs (2026-07-14) --------------------------------------
# Brand changes / M&A / reserved accounts the CRM hasn't caught up to yet. These
# hard-block DETERMINISTICALLY, independent of SF name/website matching — which can
# both over-match (e.g. "Siemens Energy" hitting the separate "Siemens" account) and
# under-match a brand-new name that has no SF record yet. Keep each entry tight
# (unique brand tokens) so it can't false-positive on unrelated companies.
MANUAL_ROE_BLOCKS = [
    {
        "name_tokens": ["omterra"],   # normalized-substring match on the company name
        "domain_stems": ["omterra"],  # first DNS label (omterra.com / .energy / .io ...)
        "detail": "MANUAL_BLOCK_REBRAND",
        "reason": ("Omterra is the Siemens Energy / Siemens Gamesa rebrand (announced 2026-07). "
                   "Siemens Energy is a reserved datacenterHawk account (Hank) — do NOT prospect. "
                   "(This is NOT the separate 'Siemens' Current Customer account.)"),
    },
]


def _manual_roe_block(company_name, domain=None):
    """Return the matching manual carve-out dict if this input is explicitly locked
    down (by brand-name token or domain stem), else None."""
    cn = re.sub(r"[^a-z0-9]", "", (company_name or "").lower())
    stem = ""
    if domain:
        d = _normalize_domain(domain)
        stem = d.split(".")[0] if d else ""
    for b in MANUAL_ROE_BLOCKS:
        if cn and any(tok in cn for tok in b["name_tokens"] if tok):
            return b
        if stem and stem in b["domain_stems"]:
            return b
    return None


# Persona keywords that callers sometimes mis-pass as the `domain` arg (see the
# 2026-04-28 outbound-pipeline argparse bug). When the value matches one of these
# or otherwise doesn't look like a domain (no dot), treat it as no-domain instead
# of poisoning ROE with a junk Website LIKE search and a junk filter input.
_PERSONA_KEYWORDS = {
    "vendor", "provider", "user", "investor", "consultant",
    "broker", "other", "prospect", "customer", "analyst",
}


_FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com",
    "gmx.com", "gmx.net", "mail.com", "zoho.com", "yandex.com", "pm.me",
    "comcast.net", "sbcglobal.net", "verizon.net", "att.net", "cox.net",
    "btinternet.com", "rocketmail.com", "fastmail.com",
}

# Non-company placeholders the inbound pipeline writes when it can't identify a
# company (personal email, unrecognized sender). These must never drive a name
# search or lead-Company search, or a no-company inbound fuzzy-matches unrelated
# accounts/leads and produces bogus ROE blocks.
_SENTINEL_COMPANY_MARKERS = ("company not found", "personal email")


def _is_freemail(value):
    """True iff value is a personal / public email provider domain (not a company)."""
    if not value:
        return False
    return _normalize_domain(value) in _FREEMAIL_DOMAINS


def _is_sentinel_company(name):
    """True iff name is a non-company placeholder (e.g. '\u274c Company Not Found')."""
    if not name:
        return True
    t = name.strip().lower().lstrip("\u274c").strip()
    if not t:
        return True
    return any(m in t for m in _SENTINEL_COMPANY_MARKERS)


def _is_valid_domain(value):
    """True iff value looks like a real *company* domain: has a dot, isn't a persona
    keyword, and isn't a personal/freemail provider (gmail.com, etc.)."""
    if not value:
        return False
    s = _normalize_domain(value)
    if not s or "." not in s:
        return False
    if s in _PERSONA_KEYWORDS:
        return False
    if s in _FREEMAIL_DOMAINS:
        return False
    return True


def _name_matches_as_word(account_name, search_term):
    """True iff `search_term` appears as a whole word in `account_name`.

    Prevents short search terms like 'Pace' from matching 'Aero**space**',
    'Mani**space** Capital', etc., when the prospect has no domain we can use
    as a disambiguator. `\\b` boundaries handle hyphens, ampersands, parens.
    """
    if not account_name or not search_term:
        return False
    pattern = r"\b" + re.escape(search_term) + r"\b"
    return bool(re.search(pattern, account_name, re.IGNORECASE))


# Corporate suffixes stripped during name normalization. Conservative set — only common
# legal-entity tags so "Acme Corp" / "Acme Inc" / "Acme LLC" all collapse to "acme".
_NAME_SUFFIXES = {
    "inc", "inc.", "incorporated",
    "llc", "l.l.c.", "lp", "l.p.", "llp", "l.l.p.",
    "ltd", "ltd.", "limited",
    "corp", "corp.", "corporation",
    "co", "co.", "company",
    "gmbh", "ag", "sa", "s.a.", "nv", "n.v.", "bv", "b.v.",
    "plc",
    "holdings", "group",
}


def _normalize_company_name(name):
    """Normalize a company name for matching: lowercase, drop whitespace + hyphens + periods,
    strip a trailing legal-entity suffix.

    Catches the variant pairs ROE used to miss:
      'Hut 8'   <-> 'HUT8'
      'd-Matrix' <-> 'd Matrix' <-> 'DMatrix'
      'Acme Corp' <-> 'Acme Inc' <-> 'Acme'

    Keeps & and other punctuation that distinguishes companies (so 'S&C Electric' stays
    distinct from 'SC Electric'). Returns the empty string for None/empty input.
    """
    if not name:
        return ""
    s = name.lower().strip()
    # Strip a trailing legal-entity suffix once (handles "Acme, Inc." or "Acme LLC").
    parts = re.split(r"[\s,]+", s)
    while parts and parts[-1].rstrip(".") in _NAME_SUFFIXES:
        parts.pop()
    s = " ".join(parts)
    # Collapse whitespace, hyphens, periods (the chars that produce variant spellings).
    s = re.sub(r"[\s\-\.]+", "", s)
    return s


def _load_anthropic_key_for_match():
    """Load Anthropic key for Opus account-matching. Openclaw config first (TCC-safe)."""
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


def opus_match_accounts(prospect_company, candidates, prospect_domain=None, prospect_signal=None):
    """Ask Opus which SF accounts represent the same data-center-industry company as the
    prospect. Used as a fallback when regex/normalization can't decide.

    The whole point: regex misses cases like 'QTS Realty Trust' (corporate filing name) ==
    'QTS Data Centers' (marketing name). Opus has the context to recognize them as the
    same company — that's the gap we're filling.

    Args:
      prospect_company: the company name from the pipeline
      candidates: list of dicts with id, name, website, customer_designation
      prospect_domain: optional, the domain we know for the prospect (helps disambiguate)
      prospect_signal: optional, the news signal that triggered this prospect

    Returns:
      Set of SF account IDs that match. Empty set if Opus declines or call fails.
    """
    if not candidates:
        return set()
    key = _load_anthropic_key_for_match()
    if not key:
        log("Opus match: no Anthropic key, skipping")
        return set()

    candidates_str = "\n".join(
        f"  - id={c['id']}  name=\"{c.get('name','')}\"  website={c.get('website') or '(none)'}  designation={c.get('customer_designation') or '(blank)'}"
        for c in candidates
    )
    extra_context = ""
    if prospect_domain:
        extra_context += f"Prospect domain: {prospect_domain}\n"
    if prospect_signal:
        extra_context += f"Prospect news signal: {prospect_signal[:300]}\n"

    prompt = (
        f"I'm doing a Salesforce account match for a data center industry prospect.\n\n"
        f"Prospect company: {prospect_company}\n"
        f"{extra_context}\n"
        f"Candidate SF accounts (these all share some name overlap with the prospect):\n{candidates_str}\n\n"
        "Decide which (if any) of these SF accounts represent the SAME real-world company as "
        f"'{prospect_company}', as it operates in the data center industry.\n\n"
        "Match if: the candidate is the same legal entity, a marketing/brand name vs corporate "
        "filing name (e.g. 'QTS Realty Trust' = 'QTS Data Centers'), a parent/subsidiary in the "
        "DC space, or a recently-renamed version of the same company.\n\n"
        "Do NOT match: similarly-named but distinct companies (e.g. 'Compass Datacenters' the DC "
        "operator is NOT 'Compass Group India' the food services firm); coincidental word overlap; "
        "different industries.\n\n"
        "Output ONLY a JSON object: {\"matching_ids\": [\"<id>\", ...], \"reason\": \"<one short sentence>\"}. "
        "Empty list if none match."
    )

    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({
                "model": "claude-opus-4-8",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            }).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, context=ctx, timeout=30)
        result = json.loads(resp.read())
        text = (result.get("content") or [{}])[0].get("text", "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            log(f"Opus match: no JSON in response: {text[:200]}")
            return set()
        parsed = json.loads(m.group())
        ids = set(parsed.get("matching_ids", []))
        reason = parsed.get("reason", "")
        if ids:
            log(f"Opus matched {len(ids)} account(s) for '{prospect_company}': {reason}")
        else:
            log(f"Opus matched none for '{prospect_company}': {reason}")
        return ids
    except Exception as e:
        log(f"Opus match call failed: {e}")
        return set()


def _name_matches_normalized(account_name, search_term):
    """True iff search_term matches account_name as a whole word, allowing for
    case/whitespace/hyphen variation. Three checks, any of which is sufficient:

      (a) Full normalized equality — catches 'HUT8' <-> 'Hut 8', 'Acme Inc' <-> 'Acme'.
      (b) Token-level normalized equality — catches 'Hut-8 Mining' <-> 'Hut 8'
          ('hut8' is a token of 'hut-8 mining' once tokens are normalized).
      (c) Original word-boundary regex on raw text — preserves the existing rejection
          of 'Pace' inside 'Aerospace' or 'Pacemaker'.

    Note: this function intentionally does NOT do "starts-with" on the normalized blob
    ('Pacemaker'.startswith('pace') would be a false positive). Token-level matching
    is the right granularity.
    """
    if not account_name or not search_term:
        return False
    n_search = _normalize_company_name(search_term)
    n_acc = _normalize_company_name(account_name)
    # (a) full normalized equality
    if n_search and n_search == n_acc:
        return True
    # (b) token-level normalized equality. Split the account name on whitespace and
    # punctuation that produces variant spellings, normalize each token individually,
    # and look for an exact match on the normalized search.
    if n_search:
        tokens = re.split(r"[\s,/&]+", account_name)
        for tok in tokens:
            tok_norm = re.sub(r"[\s\-\.]+", "", tok.lower().strip())
            if tok_norm and tok_norm == n_search:
                return True
    # (c) original word-boundary check on raw text (catches 'Pace Industries' / 'Pace'
    # without resurrecting Pace-in-Aerospace).
    return _name_matches_as_word(account_name, search_term)


def _accept_name_match(account_website, prospect_search_domains):
    """For name-LIKE / related matches: reject accounts whose website clearly points elsewhere.

    Accept when: no prospect domain known, account has no website set, or account's website
    overlaps with any prospect search domain. This blocks false positives like
    "Value Aligned Research Advisors" (var-advisors.com) being treated as the same company
    as Aligned Data Centers (aligneddc.com).
    """
    if not prospect_search_domains:
        return True
    site = _normalize_domain(account_website)
    if not site:
        return True
    for d in prospect_search_domains:
        d_norm = _normalize_domain(d)
        if d_norm and (d_norm in site or site in d_norm):
            return True
    return False


def find_accounts(company_name, domain=None):
    """Search SF for accounts by DOMAIN FIRST, then name as fallback."""
    accounts = {}

    # Defensive: callers occasionally pass non-domain strings (e.g. a persona
    # keyword "vendor" via an upstream argparse bug). Treat those as no-domain
    # instead of poisoning ROE with a `Website LIKE '%vendor%'` search.
    if domain and not _is_valid_domain(domain):
        log(f"Ignoring invalid domain input {domain!r} for {company_name} (treating as no-domain)")
        domain = None

    # Build the prospect domain set once — used for both lookup and filtering
    # name-LIKE matches that refer to unrelated companies sharing common words.
    search_domains = []
    if domain:
        domain_clean = domain.replace("'", "\\'").replace("https://", "").replace("http://", "").replace("www.", "")
        search_domains.append(domain_clean)
        mapped = PARENT_DOMAIN_MAP.get(domain_clean)
        if mapped and mapped != domain_clean:
            search_domains.append(mapped)

    # ALWAYS search by domain first — most reliable match
    if domain:
        likes = " OR ".join([f"Website LIKE '%{d}%'" for d in search_domains])
        records = sf_query(
            f"SELECT Id, Name, Website, OwnerId, Owner.Name, Customer_Designation__c "
            f"FROM Account WHERE {likes} LIMIT 20"
        )
        for r in records:
            accounts[r["Id"]] = {
                "id": r["Id"],
                "name": r["Name"],
                "website": r.get("Website", ""),
                "owner_id": r.get("OwnerId", ""),
                "owner_name": r.get("Owner", {}).get("Name", "") if r.get("Owner") else "",
                "customer_designation": r.get("Customer_Designation__c", ""),
                "match_type": "website",
            }

        # Also check Contacts and Leads by email domain
        records = sf_query(
            f"SELECT AccountId, Account.Name, Account.Website, Account.OwnerId, Account.Owner.Name, Account.Customer_Designation__c "
            f"FROM Contact WHERE Email LIKE '%@{domain_clean}' AND AccountId != null LIMIT 5"
        )
        for r in records:
            aid = r.get("AccountId", "")
            if aid and aid not in accounts:
                acct = r.get("Account", {}) or {}
                accounts[aid] = {
                    "id": aid,
                    "name": acct.get("Name", ""),
                    "website": acct.get("Website", ""),
                    "owner_id": acct.get("OwnerId", ""),
                    "owner_name": acct.get("Owner", {}).get("Name", "") if acct.get("Owner") else "",
                    "customer_designation": acct.get("Customer_Designation__c", ""),
                    "match_type": "contact_domain",
                }

    # Also search by name (some accounts may not have a website set).
    # We OR together a few LIKE variants so the SOQL candidate set is broad enough that
    # post-filtering can find the right match across naming variations:
    #   - Original spelling                           ('Compass Datacenters')
    #   - Whitespace removed                          ('CompassDatacenters'   -> matches 'HUT8')
    #   - Hyphenated                                  ('Compass-Datacenters')
    #   - First significant token alone               ('Compass')             <- catches
    #     'Compass DATA CENTERS' (with space) and 'QTS DATA CENTERS' when the
    #     pipeline-supplied name was 'QTS Realty Trust' or 'Compass Datacenters'.
    # The post-filter (_name_matches_normalized + _accept_name_match) then narrows the
    # broader candidate set back down to true matches, so the wider SOQL doesn't cause
    # false-positive blocks.
    # A non-company sentinel ('\u274c Company Not Found', personal-email placeholder)
    # must not drive a name-LIKE search that fuzzy-matches unrelated accounts.
    if not company_name or _is_sentinel_company(company_name):
        return list(accounts.values())
    name_clean = company_name.replace("'", "\\'")
    name_no_ws = re.sub(r"[\s\-\.]+", "", name_clean)
    name_variants = {name_clean, name_no_ws}
    if " " in name_clean:
        name_variants.add(name_clean.replace(" ", "-"))
    # First significant token — skip generic words that would over-fetch.
    _GENERIC_FIRST_WORDS = {
        "the", "a", "an", "data", "datacenter", "datacenters", "energy", "power",
        "global", "group", "holdings", "international", "national", "american",
        "north", "south", "east", "west", "new", "first", "next",
    }
    # Also add a TLD-stripped variant so a 'Nodiac.ai' prospect string matches a
    # 'Nodiac' account (Apollo/domain bootstrapping often hands us 'company.tld').
    _tld_stripped = re.sub(r"\.(ai|com|io|co|net|org|app|dev)$", "", name_clean, flags=re.I).strip()
    if _tld_stripped and _tld_stripped != name_clean:
        name_variants.add(_tld_stripped)
    first_significant_tok = None
    for tok in re.split(r"[\s\-\.]+", name_clean):
        tok_clean = tok.strip()
        if len(tok_clean) >= 3 and tok_clean.lower() not in _GENERIC_FIRST_WORDS:
            first_significant_tok = tok_clean
            if tok_clean not in name_variants:
                name_variants.add(tok_clean)
            break
    name_likes = " OR ".join(f"Name LIKE '%{v}%'" for v in sorted(name_variants) if v)
    records = sf_query(
        f"SELECT Id, Name, Website, OwnerId, Owner.Name, Customer_Designation__c "
        f"FROM Account WHERE {name_likes} LIMIT 25"
    )
    name_added_count = 0
    rejected_for_first_word_fallback = []
    for r in records:
        if r["Id"] in accounts:
            accounts[r["Id"]]["match_type"] = "domain+name"
            continue
        if not _accept_name_match(r.get("Website", ""), search_domains):
            log(f"Rejecting name match '{r['Name']}' (website {r.get('Website','')!r}) — does not match prospect domain {search_domains}")
            continue
        if not _name_matches_normalized(r["Name"], company_name):
            # Hold for the first-word fallback below if normalized match fails.
            rejected_for_first_word_fallback.append(r)
            log(f"Rejecting name match '{r['Name']}' — '{company_name}' is not a whole-word or normalized match")
            continue
        accounts[r["Id"]] = {
            "id": r["Id"],
            "name": r["Name"],
            "website": r.get("Website", ""),
            "owner_id": r.get("OwnerId", ""),
            "owner_name": r.get("Owner", {}).get("Name", "") if r.get("Owner") else "",
            "customer_designation": r.get("Customer_Designation__c", ""),
            "match_type": "name",
        }
        name_added_count += 1

    # Opus fallback for semantic matches the regex/normalization layer can't decide.
    # Catches: corporate vs marketing names ('QTS Realty Trust' = 'QTS Data Centers'),
    # parent/subsidiary in the DC space, recent renames, abbreviations.
    # Only fires when no regex match was accepted AND there are still rejected candidates
    # to consider — this keeps the cost (1 Opus call) bounded to ambiguous cases.
    if name_added_count == 0 and rejected_for_first_word_fallback:
        candidates_for_opus = [
            {
                "id": r["Id"],
                "name": r.get("Name", ""),
                "website": r.get("Website", ""),
                "customer_designation": r.get("Customer_Designation__c", ""),
            }
            for r in rejected_for_first_word_fallback
        ]
        opus_ids = opus_match_accounts(
            company_name,
            candidates_for_opus,
            prospect_domain=domain,
        )
        for r in rejected_for_first_word_fallback:
            if r["Id"] in opus_ids and r["Id"] not in accounts:
                accounts[r["Id"]] = {
                    "id": r["Id"],
                    "name": r["Name"],
                    "website": r.get("Website", ""),
                    "owner_id": r.get("OwnerId", ""),
                    "owner_name": r.get("Owner", {}).get("Name", "") if r.get("Owner") else "",
                    "customer_designation": r.get("Customer_Designation__c", ""),
                    "match_type": "opus_semantic",
                }

    # Search for related/parent/subsidiary accounts
    # Extract root company name (e.g., "Rolls-Royce" from "Rolls-Royce mtu")
    # and search for siblings
    # Skip for massive companies where name-based sibling search matches too many accounts
    SKIP_RELATED_NAMES = {
        "nvidia", "apple", "google", "amazon", "microsoft", "meta", "oracle",
        "tesla", "intel", "ibm", "dell", "cisco", "hp", "hpe", "adobe",
        "vmware", "samsung",
    }
    if accounts:
        found_names = [a["name"] for a in accounts.values()]
        # Try to find the root name — longest common prefix among words
        root_words = company_name.replace("-", " ").replace("_", " ").split()
        for word in root_words:
            if len(word) > 5 and word.lower() not in ("data", "the", "group", "inc", "llc", "ltd", "corp", "energy", "power", "global", "systems", "solutions", "bloom", "solar", "green", "edge", "prime", "pure", "ventures", "capital", "partners", "holdings", "properties", "realty", "technologies", "technology", "management", "industries", "international", "associates", "digital", "labs", "consulting", "advisors", "infrastructure", "development") \
                    and word.lower() not in SKIP_RELATED_NAMES:
                # Search for other accounts with this root word
                word_clean = word.replace("'", "\\'")
                siblings = sf_query(
                    f"SELECT Id, Name, Website, OwnerId, Owner.Name, Customer_Designation__c "
                    f"FROM Account WHERE Name LIKE '%{word_clean}%' LIMIT 10"
                )
                for r in siblings:
                    if r["Id"] in accounts:
                        continue
                    if not _accept_name_match(r.get("Website", ""), search_domains):
                        log(f"Rejecting related account '{r['Name']}' (website {r.get('Website','')!r}) — does not match prospect domain {search_domains}")
                        continue
                    if not _name_matches_normalized(r["Name"], word):
                        log(f"Rejecting related account '{r['Name']}' — '{word}' is not a whole-word or normalized match")
                        continue
                    accounts[r["Id"]] = {
                        "id": r["Id"],
                        "name": r["Name"],
                        "website": r.get("Website", ""),
                        "owner_id": r.get("OwnerId", ""),
                        "owner_name": r.get("Owner", {}).get("Name", "") if r.get("Owner") else "",
                        "customer_designation": r.get("Customer_Designation__c", ""),
                        "match_type": "related",
                    }
                    log(f"Found related account: {r['Name']}")
                break  # Only search on first significant word to avoid noise
            elif word.lower() in SKIP_RELATED_NAMES:
                log(f"Skipping related account search for common company name: {word}")
                break

    return list(accounts.values())


def check_leads(domain, cutoff_date, company_name=None):
    """Surface ALL Leads matching by email domain OR company name — ANY age, ANY status.

    Per the ROE doc ("Thoroughly research Salesforce by email, name, company"),
    leads must be findable even when no domain is known — so we also match on
    Lead.Company. Returns (count, records).

    Leads are surfaced for RESEARCH, not blocking — so this is intentionally NOT
    gated by CreatedDate or Status. An old lead (even pre-window or Unqualified)
    can still carry valuable context: who the CEO is, whether it converted into an
    opportunity, prior owner, etc. The blocking decision stays windowed in
    decide_roe(); over-surfacing here is preferred to missing an associated lead.
    (cutoff_date kept in the signature for caller compatibility; unused.)"""
    clauses = []
    if domain:
        d = domain.replace("'", "\\'")
        clauses.append(f"Email LIKE '%@{d}'")
    if company_name and not _is_sentinel_company(company_name):
        raw = company_name.replace("'", "\\'").strip()
        # Strip a trailing domain TLD / legal suffix so a 'Nodiac.ai' prospect
        # string still matches a 'Nodiac' lead Company, and vice-versa.
        base = re.sub(r"\.(ai|com|io|co|net|org|app|dev|inc|llc)$", "", raw, flags=re.I).strip()
        for term in {raw, base}:
            if len(term) >= 3:
                clauses.append(f"Company LIKE '%{term}%'")
    if not clauses:
        return 0, []
    records = sf_query(
        f"SELECT Id, Name, Company, Email, Status, OwnerId, Owner.Name, CreatedDate, "
        f"LastActivityDate, IsConverted, ConvertedOpportunityId, ConvertedAccountId, "
        f"ConvertedContactId "
        f"FROM Lead WHERE ({' OR '.join(clauses)}) "
        f"ORDER BY CreatedDate DESC LIMIT 50"
    )
    seen, out = set(), []
    for r in records:
        if r["Id"] not in seen:
            seen.add(r["Id"])
            out.append(r)
    return len(out), out


def check_emails(domain, cutoff_date):
    """Check for recent EmailMessage activity by domain (inbound or outbound)."""
    if not domain:
        return 0, []
    d = domain.replace("'", "\\'")
    records = sf_query(
        f"SELECT Id, Subject, MessageDate, FromAddress, FromName, ToAddress, CreatedDate "
        f"FROM EmailMessage "
        f"WHERE (ToAddress LIKE '%@{d}%' OR FromAddress LIKE '%@{d}%' OR CcAddress LIKE '%@{d}%') "
        f"AND CreatedDate >= {cutoff_date} "
        f"ORDER BY CreatedDate DESC LIMIT 10"
    )
    return len(records), records


# --- "Self": the rep running outbound ---
# ROE is collision-prevention BETWEEN reps. You cannot collide with yourself, so
# activity YOU performed never blocks you (it is surfaced so you don't double-tap
# the same contact). PER POLICY: account OWNERSHIP is irrelevant to ROE — only who
# performed the ACTIVITY matters. The one non-activity carve-out is Current
# Customer, which hard-blocks regardless (handled in roe_check, not here).
SELF_EMAIL = "you@example.com"
SELF_OWNER_IDS = {"005UM000004VyQbYAK"}            # Hank Autrey
SELF_OWNER_NAMES = {"hank autrey"}
OUTREACH_FREE_DAYS = 30   # another rep outreach within this, no response => in flight (block); 30-120d => free to prospect w/ NEW contact (ROE updated 2026-07, was 60)


def _is_self_owner(owner_name=None, owner_id=None):
    """True iff the activity owner is the rep running outbound (Hank)."""
    if owner_id and owner_id in SELF_OWNER_IDS:
        return True
    if owner_name and owner_name.strip().lower() in SELF_OWNER_NAMES:
        return True
    return False


def _quote_ids(ids):
    return ",".join("'" + i.replace("'", "") + "'" for i in ids if i)


def _days_ago(iso_ts, now):
    """Whole days between an SF timestamp and `now` (naive UTC). 9999 if unparseable."""
    if not iso_ts:
        return 9999
    s = iso_ts.replace("Z", "").split("+")[0].split(".")[0]
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except Exception:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except Exception:
            return 9999
    return (now - dt).days


def gather_signals(account_ids, who_ids, domain, cutoff, now, extra_domains=None):
    """Collect ROE signals within the lookback window:
      - engagement (2-way): inbound email RESPONSE, logged Call, any Meeting/Event
      - outreach (1-way):   outbound email with no reply
      - open opportunity:   the exception (blocks regardless of recency)
    Marketing-form tasks (TaskSubtype='Task') are TRIGGERS and intentionally excluded.
    Email direction comes from EmailMessage.Incoming; self/other from the address.
    Each signal: {kind, is_self, days_ago, by, detail}.

    Email activity is detected by domain, so we search the passed-in `domain` AND
    `extra_domains` (the real email domains of contacts/leads on the matched accounts).
    That covers the case where the caller passes a wrong domain — e.g. a related
    account's website (aecomtishman.com) instead of the contact's own domain
    (tishmanspeyer.com), which false-CLEARed Tishman Speyer on 2026-07-16."""
    signals = []

    # --- Emails (direction via EmailMessage.Incoming) ---
    email_domains = set()
    if domain:
        email_domains.add(domain)
    if extra_domains:
        email_domains.update(d for d in extra_domains if d)
    seen_email = set()
    for dom in email_domains:
        d = dom.replace("'", "\\'")
        for r in sf_query(
            f"SELECT Incoming, FromAddress, ToAddress, MessageDate, CreatedDate, Subject "
            f"FROM EmailMessage "
            f"WHERE (ToAddress LIKE '%@{d}%' OR FromAddress LIKE '%@{d}%' OR CcAddress LIKE '%@{d}%') "
            f"AND CreatedDate >= {cutoff} ORDER BY CreatedDate DESC LIMIT 50"
        ):
            incoming = bool(r.get("Incoming"))
            frm = (r.get("FromAddress") or "").lower()
            to = (r.get("ToAddress") or "").lower()
            subj = (r.get("Subject") or "")[:40]
            key = (incoming, frm, to, r.get("MessageDate") or r.get("CreatedDate"), subj)
            if key in seen_email:      # same email can match on >1 domain — count once
                continue
            seen_email.add(key)
            is_self = (SELF_EMAIL in to) if incoming else (SELF_EMAIL in frm)
            signals.append({
                "kind": "response" if incoming else "outreach",
                "is_self": is_self,
                "days_ago": _days_ago(r.get("MessageDate") or r.get("CreatedDate"), now),
                "by": (to if incoming else frm),
                "detail": (f"Response from {frm}" if incoming else f"Outreach to {to}") + f" — {subj}",
            })

    # --- Logged Call / Email tasks + Meetings (by account / contact / lead) ---
    # TaskSubtype='Call' => logged call (2-way engagement). TaskSubtype='Email' =>
    # a logged email send (EAC and marketing blasts log sends as Email tasks on
    # WhatId=account), treated as another rep's 1-way outreach — same tier as an
    # outbound EmailMessage. This catches account-scoped email activity even when
    # the domain-only EmailMessage search above misses it (wrong/absent domain).
    # TaskSubtype='Task' (marketing-form submissions) stays intentionally excluded.
    scope = []
    if account_ids:
        scope.append(f"WhatId IN ({_quote_ids(account_ids)})")
    if who_ids:
        scope.append(f"WhoId IN ({_quote_ids(who_ids)})")
    if scope:
        scope_clause = " OR ".join(scope)
        for r in sf_query(
            f"SELECT Subject, TaskSubtype, CreatedDate, OwnerId, Owner.Name FROM Task "
            f"WHERE TaskSubtype IN ('Call','Email') AND CreatedDate >= {cutoff} AND ({scope_clause}) LIMIT 100"
        ):
            owner = (r.get("Owner") or {}).get("Name", "") if r.get("Owner") else ""
            if r.get("TaskSubtype") == "Call":
                signals.append({"kind": "call", "is_self": _is_self_owner(owner, r.get("OwnerId")),
                                "days_ago": _days_ago(r.get("CreatedDate"), now), "by": owner,
                                "detail": f"Call: {(r.get('Subject') or '')[:40]} by {owner}"})
            else:
                signals.append({"kind": "outreach", "is_self": _is_self_owner(owner, r.get("OwnerId")),
                                "days_ago": _days_ago(r.get("CreatedDate"), now), "by": owner,
                                "detail": f"Logged email: {(r.get('Subject') or '')[:40]} by {owner}"})
        for r in sf_query(
            f"SELECT Subject, CreatedDate, OwnerId, Owner.Name FROM Event "
            f"WHERE CreatedDate >= {cutoff} AND ({scope_clause}) LIMIT 50"
        ):
            owner = (r.get("Owner") or {}).get("Name", "") if r.get("Owner") else ""
            signals.append({"kind": "meeting", "is_self": _is_self_owner(owner, r.get("OwnerId")),
                            "days_ago": _days_ago(r.get("CreatedDate"), now), "by": owner,
                            "detail": f"Meeting: {(r.get('Subject') or '')[:40]} by {owner}"})

    # --- Open opportunities (the exception — blocks regardless of recency) ---
    if account_ids:
        for r in sf_query(
            f"SELECT Name, StageName, OwnerId, Owner.Name FROM Opportunity "
            f"WHERE IsClosed = false AND AccountId IN ({_quote_ids(account_ids)}) LIMIT 20"
        ):
            owner = (r.get("Owner") or {}).get("Name", "") if r.get("Owner") else ""
            signals.append({"kind": "open_opp", "is_self": _is_self_owner(owner, r.get("OwnerId")),
                            "days_ago": 0, "by": owner,
                            "detail": f"Open Opp: {(r.get('Name') or '')[:40]} (Stage {r.get('StageName','?')}) owner {owner}"})
    return signals


def gather_historical_context(account_ids, who_ids, domain, lead_records, now):
    """ALL-TIME research pass (no 120d cutoff) over the account + contacts + leads.

    Purely informational — these never block (blocking lives in decide_roe and stays
    windowed). The point is THOROUGHNESS: an opp that closed 2 years ago, a dormant
    CEO lead, an old email thread, or a lead that converted into an opportunity are
    all valuable context a rep should see before reaching out, even if they're far
    outside the activity window. Returns a list of human-readable context lines."""
    ctx = []

    # --- All opportunities on the account (open AND closed), any age ---
    if account_ids:
        for r in sf_query(
            f"SELECT Name, StageName, IsClosed, IsWon, CloseDate, Amount, Owner.Name "
            f"FROM Opportunity WHERE AccountId IN ({_quote_ids(account_ids)}) "
            f"ORDER BY CloseDate DESC NULLS LAST LIMIT 50"
        ):
            owner = (r.get("Owner") or {}).get("Name", "") if r.get("Owner") else ""
            if r.get("IsClosed"):
                state = "WON" if r.get("IsWon") else "Lost/Closed"
            else:
                state = f"OPEN (Stage {r.get('StageName','?')})"
            amt = f" ${int(r['Amount']):,}" if r.get("Amount") else ""
            ctx.append(f"Opp: {(r.get('Name') or '')[:50]} — {state}{amt} "
                       f"close {str(r.get('CloseDate') or '?')[:10]} owner {owner}")

    # --- Opportunities a matched lead converted into (may live on a DIFFERENT account) ---
    conv_opp_ids = [l.get("ConvertedOpportunityId") for l in (lead_records or [])
                    if l.get("ConvertedOpportunityId")]
    if conv_opp_ids:
        for r in sf_query(
            f"SELECT Name, StageName, IsClosed, IsWon, CloseDate, Owner.Name, Account.Name "
            f"FROM Opportunity WHERE Id IN ({_quote_ids(conv_opp_ids)}) LIMIT 50"
        ):
            owner = (r.get("Owner") or {}).get("Name", "") if r.get("Owner") else ""
            acct = (r.get("Account") or {}).get("Name", "") if r.get("Account") else ""
            state = ("WON" if r.get("IsWon") else "Lost/Closed") if r.get("IsClosed") \
                else f"OPEN (Stage {r.get('StageName','?')})"
            ctx.append(f"Converted-lead Opp: {(r.get('Name') or '')[:50]} — {state} "
                       f"on account '{acct}' owner {owner}")

    # --- All tasks/events on account + contacts + leads, any age (counts + recent few) ---
    scope = []
    if account_ids:
        scope.append(f"WhatId IN ({_quote_ids(account_ids)})")
    if who_ids:
        scope.append(f"WhoId IN ({_quote_ids(who_ids)})")
    if scope:
        scope_clause = " OR ".join(scope)
        tasks = sf_query(
            f"SELECT Subject, TaskSubtype, CreatedDate, Owner.Name FROM Task "
            f"WHERE ({scope_clause}) ORDER BY CreatedDate DESC LIMIT 200"
        )
        if tasks:
            ctx.append(f"Tasks on file (all-time): {len(tasks)} total")
            for r in tasks[:5]:
                owner = (r.get("Owner") or {}).get("Name", "") if r.get("Owner") else ""
                d = _days_ago(r.get("CreatedDate"), now)
                ctx.append(f"  · [{r.get('TaskSubtype','?')}] {(r.get('Subject') or '')[:55]} "
                           f"({d}d ago) by {owner}")
        events = sf_query(
            f"SELECT Subject, CreatedDate, Owner.Name FROM Event "
            f"WHERE ({scope_clause}) ORDER BY CreatedDate DESC LIMIT 100"
        )
        if events:
            ctx.append(f"Events on file (all-time): {len(events)} total")
            for r in events[:3]:
                owner = (r.get("Owner") or {}).get("Name", "") if r.get("Owner") else ""
                d = _days_ago(r.get("CreatedDate"), now)
                ctx.append(f"  · {(r.get('Subject') or '')[:55]} ({d}d ago) by {owner}")

    # --- All email threads by domain, any age ---
    if domain:
        d = domain.replace("'", "\\'")
        emails = sf_query(
            f"SELECT Subject, MessageDate, FromAddress, ToAddress, Incoming FROM EmailMessage "
            f"WHERE (ToAddress LIKE '%@{d}%' OR FromAddress LIKE '%@{d}%' OR CcAddress LIKE '%@{d}%') "
            f"ORDER BY MessageDate DESC LIMIT 50"
        )
        if emails:
            ctx.append(f"Emails on file (all-time): {len(emails)} total")
            for r in emails[:3]:
                dir_ = "in" if r.get("Incoming") else "out"
                days = _days_ago(r.get("MessageDate"), now)
                ctx.append(f"  · [{dir_}] {(r.get('Subject') or '')[:55]} ({days}d ago)")

    return ctx


def decide_roe(signals):
    """Apply the ROE tiers (first match wins). Account ownership is NOT consulted.
    Returns (blocked: bool, detail: str, reasons: list, notes: list, owner: str|None)."""
    in_window = lambda s: s["days_ago"] <= LOOKBACK_DAYS
    eng_all = [s for s in signals if s["kind"] in ("response", "call", "meeting") and not s["is_self"] and in_window(s)]
    eng_other = [s for s in eng_all if s["days_ago"] <= ENGAGE_DAYS]            # <=90d => ACTIVE (block)
    eng_other_stale = [s for s in eng_all if s["days_ago"] > ENGAGE_DAYS]       # 90-120d => inactive, check prior owner (note)
    out_other = [s for s in signals if s["kind"] == "outreach" and not s["is_self"] and in_window(s)]
    opp_other = [s for s in signals if s["kind"] == "open_opp" and not s["is_self"]]
    self_sig = [s for s in signals if s["is_self"] and (s["kind"] == "open_opp" or in_window(s))]
    out_other_recent = [s for s in out_other if s["days_ago"] <= OUTREACH_FREE_DAYS]
    out_other_old = [s for s in out_other if s["days_ago"] > OUTREACH_FREE_DAYS]

    # Tier 1: another rep's open opportunity
    if opp_other:
        s = opp_other[0]
        return True, "OPEN_OPP", [f"Open opportunity owned by another rep ({s['by']}): {s['detail']}"], [], s["by"]
    # Tier 2: another rep's engagement (response / call / meeting) within ENGAGE_DAYS (90d)
    if eng_other:
        s = min(eng_other, key=lambda x: x["days_ago"])
        return True, "ACTIVE_OWNED", [f"Engagement by another rep {s['days_ago']}d ago (<{ENGAGE_DAYS}d, active) ({s['by']}): {s['detail']}"], [], s["by"]
    # Tier 3: another rep's outreach within OUTREACH_FREE_DAYS (30d, in flight, no response yet)
    if out_other_recent:
        s = min(out_other_recent, key=lambda x: x["days_ago"])
        return True, "RECENT_OUTREACH", [f"Another rep outreach {s['days_ago']}d ago (<{OUTREACH_FREE_DAYS}d, in flight) by {s['by']}: {s['detail']}"], [], s["by"]

    # CLEAR — assemble surface flags (never block on these)
    notes = []
    if eng_other_stale:
        s = min(eng_other_stale, key=lambda x: x["days_ago"])
        notes.append(f"Another rep engaged {s['days_ago']}d ago (>{ENGAGE_DAYS}d, inactive — CHECK WITH PRIOR ACCOUNT OWNER before reaching out) by {s['by']}: {s['detail']}")
    if self_sig:
        notes.append("YOUR prior activity here — use a NEW contact to avoid double-tapping: "
                     + "; ".join(s["detail"] for s in sorted(self_sig, key=lambda x: x["days_ago"])[:3]))
    if out_other_old:
        s = min(out_other_old, key=lambda x: x["days_ago"])
        notes.append(f"Another rep outreach {s['days_ago']}d ago, no response (>{OUTREACH_FREE_DAYS}d — free to prospect, use a NEW contact) by {s['by']}")
    detail = "FIND_NEW_CONTACT" if (self_sig or out_other_old or eng_other_stale) else "NO_ACTIVITY"
    return False, detail, [], notes, None


def roe_check(company_name, domain=None):
    """Full ROE check. Activity-based: blocks on another rep's engagement (90d),
    recent outreach (30d), or open opp; Current Customer hard-blocks. Account
    ownership is ignored. Your own activity never blocks (surfaced as a flag)."""
    now = datetime.utcnow()
    cutoff = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00Z")

    # Personal/freemail addresses (gmail.com, etc.) are NOT company domains. Never
    # let them drive account/lead/email matching: otherwise a no-company inbound is
    # matched to whatever accounts merely have a freemail contact on file (e.g.
    # Wells Fargo), whose open opps then trigger a bogus block. Treat as no-domain.
    if domain and _is_freemail(domain):
        log(f"Ignoring personal/freemail domain {domain!r} for {company_name!r} - treating as no-domain")
        domain = None

    result = {
        "company": company_name, "domain": domain,
        "checked_at": now.isoformat() + "Z", "cutoff_date": cutoff,
        "status": None, "status_detail": None,
        "accounts": [], "lead_activity": {"count": 0, "details": []},
        "blocking_reasons": [], "surface_notes": [], "signals": [], "owner": None,
        "historical_context": [],
    }

    # Step 0: manual carve-out (rebrand / M&A / reserved) — deterministic hard block
    # before any SF matching, so a brand-new name with no SF record can't slip through.
    manual = _manual_roe_block(company_name, domain)
    if manual:
        result["status"] = "BLOCKED"
        result["status_detail"] = manual["detail"]
        result["blocking_reasons"].append(manual["reason"])
        result["accounts"] = find_accounts(company_name, domain)  # best-effort, informational
        return result

    # Step 1: detection — accounts by domain/name/company
    accounts = find_accounts(company_name, domain)
    result["accounts"] = accounts

    # Step 2: Current Customer => hard block (policy carve-out, not activity-based)
    for acct in accounts:
        if (acct.get("customer_designation") or "").strip() == "Current Customer":
            result["status"] = "BLOCKED"
            result["status_detail"] = "CURRENT_CUSTOMER"
            result["blocking_reasons"].append(f"Account '{acct['name']}' is a Current Customer")
            result["owner"] = acct.get("owner_name", "")
            return result

    # Step 3: detection — leads by domain/name/company (existence alone does NOT
    # block; only activity does — but we surface them and use their ids for scope)
    lead_count, leads = check_leads(domain, cutoff, company_name)
    result["lead_activity"]["count"] = lead_count
    for l in leads:
        owner = (l.get("Owner") or {}).get("Name", "") if l.get("Owner") else ""
        age = _days_ago(l.get("CreatedDate"), now)
        conv = ""
        if l.get("IsConverted"):
            conv = " [CONVERTED" + (" → Opp" if l.get("ConvertedOpportunityId") else "") + "]"
        result["lead_activity"]["details"].append(
            f"Lead: {l.get('Name','?')} ({l.get('Email','?')}) Status: {l.get('Status','?')} "
            f"Owner: {owner} (created {age}d ago){conv}"
        )
    lead_ids = [l["Id"] for l in leads]

    account_ids = [a["id"] for a in accounts]
    contact_ids = []
    contact_domains = set()
    if account_ids:
        for r in sf_query(f"SELECT Id, Email FROM Contact WHERE AccountId IN ({_quote_ids(account_ids)}) LIMIT 200"):
            contact_ids.append(r["Id"])
            em = (r.get("Email") or "")
            if "@" in em:
                cand = em.split("@", 1)[1].strip().lower()
                if _is_valid_domain(cand):
                    contact_domains.add(cand)
    who_ids = contact_ids + lead_ids

    # Email activity is detected by domain, and the passed-in `domain` can be wrong
    # (a related account's website, e.g. aecomtishman.com for Tishman Speyer). Also
    # search the real email domains of contacts ON the matched accounts + matched
    # leads, so EmailMessage activity isn't missed on a domain mismatch.
    extra_domains = set(contact_domains)
    for l in leads:
        em = (l.get("Email") or "")
        if "@" in em:
            cand = em.split("@", 1)[1].strip().lower()
            if _is_valid_domain(cand):
                extra_domains.add(cand)

    # Self-heal: email signals can only be found by domain. If the caller didn't
    # pass one (common for name-sourced prospects), infer it from the matched
    # account's website, else from a matched lead's email — otherwise the lead/
    # account is found but its EMAIL activity stays invisible (the original bug).
    if not domain:
        for a in accounts:
            w = _normalize_domain(a.get("website", ""))
            if w and _is_valid_domain(w):
                domain = w
                break
        if not domain:
            for l in leads:
                em = (l.get("Email") or "")
                if "@" in em:
                    cand = em.split("@", 1)[1].strip().lower()
                    if _is_valid_domain(cand):
                        domain = cand
                        break
        if domain:
            result["domain"] = domain
            result["domain_inferred"] = True

    # Step 4: gather activity signals (windowed) + decide
    signals = gather_signals(account_ids, who_ids, domain, cutoff, now, extra_domains=extra_domains)
    result["signals"] = signals
    blocked, detail, reasons, notes, owner = decide_roe(signals)
    result["blocking_reasons"] = reasons
    result["surface_notes"] = notes

    # Step 4b: ALL-TIME research context (lead + account level, beyond the 120d window).
    # Informational only — never affects the block/clear decision above.
    result["historical_context"] = gather_historical_context(
        account_ids, who_ids, domain, leads, now)

    has_accounts = bool(accounts)
    if blocked:
        result["status"] = "BLOCKED"
        result["status_detail"] = detail
        result["owner"] = owner or (accounts[0].get("owner_name", "") if has_accounts else None)
    else:
        result["status"] = "CLEAR_EXISTING" if has_accounts else "CLEAR_NEW"
        if detail == "FIND_NEW_CONTACT":
            result["status_detail"] = "FIND_NEW_CONTACT"
        else:
            result["status_detail"] = "NO_ACTIVITY" if has_accounts else "NO_ACCOUNT"
        result["owner"] = accounts[0].get("owner_name", "") if has_accounts else None

    return result


def print_result(result):
    """Pretty-print a ROE check result."""
    status = result["status"]
    icon = "✅" if status.startswith("CLEAR") else "🚫"
    print(f"\n{icon}  {result['company']} — {status}")

    if result["domain"]:
        print(f"   Domain: {result['domain']}")
    print(f"   Checked: {result['checked_at'][:19]}")
    print(f"   Lookback: {LOOKBACK_DAYS} days (cutoff: {result['cutoff_date'][:10]})")

    if result["accounts"]:
        print(f"   SF Accounts: {len(result['accounts'])}")
        for a in result["accounts"]:
            designation = f" [{a.get('customer_designation', '')}]" if a.get("customer_designation") else ""
            print(f"     • {a['name']}{designation} (Owner: {a.get('owner_name', '?')}, Match: {a['match_type']})")
    else:
        print("   SF Accounts: None found")

    if result["blocking_reasons"]:
        print("   Blocking Reasons:")
        for r in result["blocking_reasons"]:
            print(f"     🚫 {r}")

    if result.get("lead_activity", {}).get("count", 0) > 0:
        print(f"   Leads on file (all-time): {result['lead_activity']['count']}")
        for d in result["lead_activity"]["details"]:
            print(f"     • {d}")

    # Show activity details for blocked accounts
    for a in result.get("accounts", []):
        findings = a.get("findings", {})
        details = findings.get("details", [])
        if details:
            print(f"   Activity on '{a['name']}':")
            for d in details[:5]:
                print(f"     • {d}")

    if result.get("historical_context"):
        print(f"   Historical context (beyond {LOOKBACK_DAYS}d window — informational, non-blocking):")
        for d in result["historical_context"]:
            print(f"     • {d}")

    if result["owner"]:
        print(f"   Owner: {result['owner']}")

    print()


def save_result(result):
    """Append result to state file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())

    key = result["company"].lower().strip()
    state[key] = result
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def batch_check(csv_path):
    """Check multiple companies from a CSV (columns: company_name, domain).
    Sequential — one roe_check() per row. Slower but full detail per company.
    For large batches (100+) use bulk_check() via --bulk instead (50x faster)."""
    import csv
    results = {"clear": [], "blocked": [], "errors": []}

    with open(csv_path) as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for row in reader:
            if not row or not row[0].strip():
                continue
            company = row[0].strip()
            domain = row[1].strip() if len(row) > 1 else None

            try:
                result = roe_check(company, domain)
                print_result(result)
                save_result(result)

                if result["status"].startswith("CLEAR"):
                    results["clear"].append(result)
                else:
                    results["blocked"].append(result)
            except Exception as e:
                print(f"ERROR checking {company}: {e}")
                results["errors"].append({"company": company, "error": str(e)})

    print(f"\n=== BATCH SUMMARY ===")
    print(f"CLEAR:   {len(results['clear'])}")
    print(f"BLOCKED: {len(results['blocked'])}")
    print(f"ERRORS:  {len(results['errors'])}")
    return results


def bulk_check(csv_path, output_json_path=None):
    """Fast bulk ROE — one bulk SOQL per dimension instead of one per company.
    Same rules as roe_check(): Task/Event/Opp in last 120d, Lead, EmailMessage,
    Customer_Designation__c, parent-domain map. ~50x faster for 100+ companies.
    Output: dict of {company_name: {status, blocking_reasons, ...}} + writes optional JSON."""
    import csv
    import subprocess as _sub
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    now = _dt.now(_tz.utc)
    cutoff_activity = (now - _td(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00Z")
    cutoff_opp      = (now - _td(days=OPP_REVISIT_DAYS)).strftime("%Y-%m-%dT00:00:00Z")
    parent_map = PARENT_DOMAIN_MAP

    def _sf(soql):
        r = _sub.run(["sf","data","query","--query",soql,"--json","-o","you@example.com"],
                     capture_output=True, text=True, timeout=120)
        try:
            return json.loads(r.stdout).get("result",{}).get("records",[])
        except Exception:
            return []

    def _chunks(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i:i+n]

    inputs = []
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            d = (row.get("domain") or "").lower().strip()
            search = [d]
            if d and d in parent_map:
                search.append(parent_map[d])
            inputs.append({"name": row["company_name"], "domain": d, "search_domains": search})
    log(f"bulk_check: {len(inputs)} input orgs")

    # Step 1: find matching accounts by website + name (batched)
    accounts_by_domain, accounts_by_name = {}, {}
    for batch in _chunks(inputs, 40):
        all_search = sorted({d for i in batch for d in i["search_domains"] if d})
        if not all_search: continue
        likes = " OR ".join([f"Website LIKE '%{d}%'" for d in all_search])
        for r in _sf(f"SELECT Id, Name, Website, Customer_Designation__c FROM Account WHERE {likes} LIMIT 2000"):
            w = (r.get("Website") or "").lower()
            for i in batch:
                for d in i["search_domains"]:
                    if d and d in w:
                        accounts_by_domain.setdefault(i["domain"], []).append(r)
                        break
    for batch in _chunks(inputs, 30):
        clauses = [f"Name LIKE '%{i['name'].replace(chr(39),chr(92)+chr(39)).replace('%','')}%'" for i in batch]
        for r in _sf(f"SELECT Id, Name, Website, Customer_Designation__c FROM Account WHERE {' OR '.join(clauses)} LIMIT 2000"):
            rn = (r["Name"] or "").lower()
            for i in batch:
                if i["name"].lower() in rn:
                    accounts_by_name.setdefault(i["name"].lower(), []).append(r)

    input_accounts, customer_match = {}, {}
    for i in inputs:
        ids = set(); cust = []
        for a in accounts_by_domain.get(i["domain"], []) + accounts_by_name.get(i["name"].lower(), []):
            ids.add(a["Id"])
            if (a.get("Customer_Designation__c") or "").strip() == "Current Customer":
                cust.append(a["Name"])
        input_accounts[i["name"]] = ids
        if cust: customer_match[i["name"]] = cust

    all_ids = sorted({aid for ids in input_accounts.values() for aid in ids})

    # Steps 2-4: activity queries on matched accounts
    has_task, has_event, has_opp = set(), set(), set()
    for batch in _chunks(all_ids, 200):
        if not batch: continue
        idstr = "','".join(batch)
        for r in _sf(f"SELECT AccountId, WhatId FROM Task WHERE CreatedDate >= {cutoff_activity} AND (AccountId IN ('{idstr}') OR WhatId IN ('{idstr}')) LIMIT 10000"):
            if r.get("AccountId"): has_task.add(r["AccountId"])
            w = r.get("WhatId"); has_task.add(w) if w and w.startswith("001") else None
        for r in _sf(f"SELECT AccountId, WhatId FROM Event WHERE CreatedDate >= {cutoff_activity} AND (AccountId IN ('{idstr}') OR WhatId IN ('{idstr}')) LIMIT 10000"):
            if r.get("AccountId"): has_event.add(r["AccountId"])
            w = r.get("WhatId"); has_event.add(w) if w and w.startswith("001") else None
    for batch in _chunks(all_ids, 500):
        if not batch: continue
        idstr = "','".join(batch)
        for r in _sf(f"SELECT AccountId FROM Opportunity WHERE AccountId IN ('{idstr}') AND (IsClosed = false OR CreatedDate >= {cutoff_opp}) LIMIT 5000"):
            if r.get("AccountId"): has_opp.add(r["AccountId"])

    # Step 5: leads + emails by domain
    unique_doms = sorted({i["domain"] for i in inputs if i["domain"]})
    leads_by_dom, emails_by_dom = {}, {}
    for batch in _chunks(unique_doms, 40):
        likes = " OR ".join([f"Email LIKE '%@{d}'" for d in batch])
        for r in _sf(f"SELECT Id, Email FROM Lead WHERE ({likes}) AND CreatedDate >= {cutoff_activity} AND IsConverted = false LIMIT 2000"):
            e = (r.get("Email") or "").lower()
            for d in batch:
                if e.endswith("@"+d): leads_by_dom.setdefault(d, []).append(r)
    for batch in _chunks(unique_doms, 30):
        clauses = []
        for d in batch:
            clauses.extend([f"ToAddress LIKE '%@{d}%'", f"FromAddress LIKE '%@{d}%'", f"CcAddress LIKE '%@{d}%'"])
        for r in _sf(f"SELECT Id, ToAddress, FromAddress, CcAddress FROM EmailMessage WHERE ({' OR '.join(clauses)}) AND CreatedDate >= {cutoff_activity} LIMIT 5000"):
            # per-domain attribution: count an email only for the domain(s) it actually contains.
            # (The old code attributed every row to every domain in the chunk -> massive phantom
            #  over-blocking, e.g. 0-email domains inheriting their chunkmates' totals. Fixed 2026-07-29.)
            blob = ((r.get("ToAddress") or "") + " " + (r.get("FromAddress") or "") + " " + (r.get("CcAddress") or "")).lower()
            for d in batch:
                if ("@" + d) in blob:
                    emails_by_dom.setdefault(d, []).append(r)

    # Reconcile
    results = []
    for i in inputs:
        reasons = []
        matched = input_accounts.get(i["name"], set())
        # Manual carve-out (rebrand / M&A / reserved) — deterministic, independent of matching.
        manual = _manual_roe_block(i["name"], i["domain"])
        if manual:
            reasons.append(manual["reason"])
        if i["name"] in customer_match:
            for n in customer_match[i["name"]]:
                reasons.append(f"Current Customer: {n}")
        if matched:
            if matched & has_task: reasons.append(f"Task in last {LOOKBACK_DAYS}d on {len(matched & has_task)} matched acct(s)")
            if matched & has_event: reasons.append(f"Event in last {LOOKBACK_DAYS}d on {len(matched & has_event)} matched acct(s)")
            if matched & has_opp: reasons.append(f"Open/recent Opp on {len(matched & has_opp)} matched acct(s)")
        if i["domain"] in leads_by_dom:
            reasons.append(f"{len(leads_by_dom[i['domain']])} active Lead(s) @{i['domain']}")
        if i["domain"] in emails_by_dom:
            reasons.append(f"{len(emails_by_dom[i['domain']])} EmailMessage(s) involving @{i['domain']} in last {LOOKBACK_DAYS}d")

        results.append({
            "company": i["name"], "domain": i["domain"],
            "status": "BLOCKED" if reasons else "CLEAR",
            "blocking_reasons": reasons,
            "matched_account_ids": list(matched),
            "checked_at": now.isoformat(),
        })

    clear_n = sum(1 for r in results if r["status"] == "CLEAR")
    blocked_n = len(results) - clear_n
    print(f"=== BULK ROE RESULTS ===")
    print(f"CLEAR:   {clear_n}")
    print(f"BLOCKED: {blocked_n}")
    print("\nCLEAR list:")
    for r in results:
        if r["status"] == "CLEAR":
            print(f"  {r['company']:<42} {r['domain']}")

    if output_json_path:
        from pathlib import Path as _P
        _P(output_json_path).write_text(json.dumps(results, indent=2))
        print(f"\nSaved: {output_json_path}")

    return results


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    if "--batch" in sys.argv:
        idx = sys.argv.index("--batch")
        csv_path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        if not csv_path:
            print("Usage: roe-check.py --batch /path/to/companies.csv")
            sys.exit(1)
        batch_check(csv_path)
        return

    if "--bulk" in sys.argv:
        idx = sys.argv.index("--bulk")
        csv_path = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        if not csv_path:
            print("Usage: roe-check.py --bulk /path/to/companies.csv [--out results.json]")
            sys.exit(1)
        out_path = None
        if "--out" in sys.argv:
            oi = sys.argv.index("--out")
            out_path = sys.argv[oi + 1] if oi + 1 < len(sys.argv) else None
        bulk_check(csv_path, output_json_path=out_path)
        return

    if "--json" in sys.argv:
        # JSON output mode
        if len(args) < 1:
            print("Usage: roe-check.py [--json] 'Company Name' ['domain.com']")
            sys.exit(1)
        company = args[0]
        domain = args[1] if len(args) > 1 else None
        result = roe_check(company, domain)
        print(json.dumps(result, indent=2, default=str))
        save_result(result)
        return

    if len(args) < 1:
        print("Usage: roe-check.py 'Company Name' ['domain.com']")
        print("       roe-check.py --batch /path/to/companies.csv")
        print("       roe-check.py --json 'Company Name' ['domain.com']")
        print("\nFlags: -v/--verbose, --json, --batch")
        sys.exit(1)

    company = args[0]
    domain = args[1] if len(args) > 1 else None

    result = roe_check(company, domain)
    print_result(result)
    save_result(result)


if __name__ == "__main__":
    main()
