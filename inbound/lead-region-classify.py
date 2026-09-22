import os
#!/usr/bin/env python3
"""lead-region-classify.py — Decide where an inbound lead CONTACT is physically based,
so the inbound processor can route foreign leads to the right regional rep:

    APAC individuals  -> Kathryn Wong   (route "kathryn")
    EMEA individuals  -> David Sandars  (route "david")
    US / Canada / unknown -> Hank keeps it (route "hank")

"Based" means where the PERSON is, NOT where the company is HQ'd. A US/Canada-based
person representing an APAC company is still Hank's (per Hank's rule 2026-07-14).

Signal stack (cheap -> rich, combined by Opus):
  1. Apollo people/match on the email -> person city/state/country + LinkedIn URL
  2. Email domain ccTLD (.sg/.my/.co.uk/.de ...) -> strong region hint
  3. Brave web search "<name> <company> location / linkedin" -> grounding snippets
  4. Opus final call: weighs all signals, returns region + route + confidence + reasoning

Output (stdout, one JSON object):
  {"region":"APAC|EMEA|NA|UNKNOWN","route":"kathryn|david|hank",
   "country":"Singapore","location":"Singapore","linkedin_url":"...",
   "confidence":"high|medium|low","reasoning":"...","signals":{...}}

Usage:
  lead-region-classify.py --email x@y.com [--name "Full Name"] [--company "Co"] [--json]

Fail-safe: on any hard error it prints a UNKNOWN/hank result (exit 0) so the caller
never blocks a lead on a research hiccup — an UNKNOWN lead just stays with Hank.
"""
import argparse
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BRAVE_KEY = os.environ.get("BRAVE_API_KEY", "")

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv


def log(msg):
    if VERBOSE:
        print(f"  [region] {msg}", file=sys.stderr)


# --- Country -> region. Kept explicit so the mapping is auditable, not vibes. ---
NA_COUNTRIES = {"united states", "usa", "us", "u.s.", "u.s.a.", "america",
                "canada"}
APAC_COUNTRIES = {
    "singapore", "malaysia", "japan", "china", "hong kong", "hongkong", "macau",
    "south korea", "korea", "taiwan", "india", "indonesia", "thailand", "vietnam",
    "philippines", "australia", "new zealand", "cambodia", "laos", "myanmar",
    "brunei", "bangladesh", "sri lanka", "pakistan", "nepal", "mongolia",
}
# EMEA = Europe + Middle East + Africa
EMEA_COUNTRIES = {
    "united kingdom", "uk", "u.k.", "england", "scotland", "wales", "ireland",
    "germany", "france", "netherlands", "holland", "spain", "italy", "sweden",
    "switzerland", "poland", "norway", "denmark", "finland", "belgium", "austria",
    "portugal", "greece", "czech republic", "czechia", "romania", "hungary",
    "luxembourg", "iceland", "croatia", "bulgaria", "slovakia", "slovenia",
    "estonia", "latvia", "lithuania", "serbia", "ukraine",
    # Middle East
    "united arab emirates", "uae", "u.a.e.", "saudi arabia", "israel", "qatar",
    "kuwait", "bahrain", "oman", "jordan", "lebanon", "turkey", "turkiye", "egypt",
    # Africa
    "south africa", "nigeria", "kenya", "morocco", "ghana", "ethiopia", "tanzania",
}

# --- ccTLD -> region. Second-strongest signal after a real person-location. ---
APAC_TLDS = {"sg", "my", "jp", "cn", "hk", "kr", "tw", "in", "id", "th", "vn",
             "ph", "au", "nz", "kh", "la", "mm", "bd", "lk", "pk", "np", "mn"}
EMEA_TLDS = {"uk", "de", "fr", "nl", "es", "it", "se", "ch", "pl", "ie", "no",
             "dk", "fi", "be", "at", "pt", "gr", "cz", "ro", "hu", "lu", "is",
             "hr", "bg", "sk", "si", "ee", "lv", "lt", "rs", "ua", "eu",
             "ae", "sa", "il", "qa", "kw", "bh", "om", "jo", "lb", "tr", "eg",
             "za", "ng", "ke", "ma", "gh"}
NA_TLDS = {"us", "ca"}

ROUTE_BY_REGION = {"APAC": "kathryn", "EMEA": "david", "NA": "hank", "UNKNOWN": "hank"}


def country_to_region(country):
    if not country:
        return None
    c = country.strip().lower()
    if c in NA_COUNTRIES:
        return "NA"
    if c in APAC_COUNTRIES:
        return "APAC"
    if c in EMEA_COUNTRIES:
        return "EMEA"
    return None


def tld_region(email):
    """Region hint from the email's ccTLD. Handles multi-part TLDs like .com.my / .co.uk."""
    if "@" not in email:
        return None, None
    domain = email.rsplit("@", 1)[1].lower().strip()
    parts = domain.split(".")
    if not parts:
        return None, None
    last = parts[-1]
    # .com.my, .co.uk -> the cc is the final label
    if last in APAC_TLDS:
        return "APAC", last
    if last in EMEA_TLDS:
        return "EMEA", last
    if last in NA_TLDS:
        return "NA", last
    return None, last  # .com/.net/.org/.io -> ambiguous


def apollo_enrich(email, first_name="", last_name=""):
    payload = {"email": email, "first_name": first_name, "last_name": last_name}
    try:
        req = urllib.request.Request(
            "https://api.apollo.io/api/v1/people/match",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "X-Api-Key": APOLLO_API_KEY, "User-Agent": "Mozilla/5.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
            p = json.loads(resp.read()).get("person", {}) or {}
        return {
            "city": p.get("city") or "",
            "state": p.get("state") or "",
            "country": p.get("country") or "",
            "linkedin_url": p.get("linkedin_url") or "",
            "title": p.get("title") or "",
        }
    except Exception as e:
        log(f"Apollo enrich error: {e}")
        return {}


def brave_search(query, count=5):
    try:
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
            {"q": query, "count": count})
        req = urllib.request.Request(
            url, headers={"X-Subscription-Token": BRAVE_KEY, "Accept": "application/json"})
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as resp:
            data = json.loads(resp.read())
        out = []
        for r in (data.get("web", {}) or {}).get("results", [])[:count]:
            title = (r.get("title") or "").strip()
            desc = re.sub(r"<[^>]+>", "", r.get("description") or "").strip()
            if title:
                out.append(f"{title} — {desc}"[:300])
        return out
    except Exception as e:
        log(f"Brave search failed ({e})")
        return []


def claude_json(prompt, model="claude-opus-4-8", max_tokens=500):
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({
            "model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }).encode(),
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=60) as resp:
        text = json.loads(resp.read())["content"][0]["text"].strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def classify(email, name, company):
    signals = {}

    # 1) Apollo person-location (the gold signal when present)
    first, last = "", ""
    if name:
        toks = name.split()
        first = toks[0] if toks else ""
        last = toks[-1] if len(toks) > 1 else ""
    ap = apollo_enrich(email, first, last)
    ap_country = ap.get("country", "")
    ap_loc = ", ".join([x for x in [ap.get("city", ""), ap.get("state", ""), ap_country] if x])
    linkedin = ap.get("linkedin_url", "")
    signals["apollo"] = {"country": ap_country, "location": ap_loc,
                         "title": ap.get("title", ""), "linkedin": linkedin}

    # 2) ccTLD hint
    tld_reg, tld = tld_region(email)
    signals["tld"] = {"region": tld_reg, "tld": tld}

    # --- Deterministic fast paths (skip Opus when a signal is unambiguous) ---
    ap_region = country_to_region(ap_country)
    if ap_region:
        # A real person-country beats everything.
        return {
            "region": ap_region, "route": ROUTE_BY_REGION[ap_region],
            "country": ap_country, "location": ap_loc or ap_country,
            "linkedin_url": linkedin, "confidence": "high",
            "reasoning": f"Apollo reports the contact is based in {ap_loc or ap_country}.",
            "signals": signals,
        }
    if tld_reg in ("APAC", "EMEA") and not ap_country:
        # A country-code company domain with no conflicting person-location.
        return {
            "region": tld_reg, "route": ROUTE_BY_REGION[tld_reg],
            "country": "", "location": f".{tld} domain",
            "linkedin_url": linkedin, "confidence": "medium",
            "reasoning": f"Email domain uses the .{tld} country code, which maps to {tld_reg}.",
            "signals": signals,
        }

    # 3) Brave grounding — only when we still don't know (ambiguous .com, no Apollo loc)
    q = " ".join([x for x in [name, company, "location office based headquarters linkedin"] if x])
    snippets = brave_search(q, count=5)
    signals["brave"] = snippets

    # 4) Opus decides from everything we gathered
    prompt = f"""You decide where an INBOUND SALES LEAD (a person) is PHYSICALLY BASED, to route them to the right regional rep.

Regions:
- NA   = the person is based in the United States or Canada.
- APAC = the person is based in Asia-Pacific (Singapore, Malaysia, Japan, China, Hong Kong, Korea, Taiwan, India, SE Asia, Australia, NZ, etc.).
- EMEA = the person is based in Europe, the Middle East, or Africa.
- UNKNOWN = you genuinely cannot tell.

CRITICAL: judge where the PERSON is, not where their company is headquartered. A US/Canada-based person who works for an APAC company is NA. Only classify as APAC/EMEA if the PERSON themselves appears to sit there.

Lead:
- Name: {name or '(unknown)'}
- Email: {email}
- Company: {company or '(unknown)'}
- Apollo person location: {ap_loc or '(none found)'}
- Email domain country code: {('.' + tld) if tld else '(generic .com/.net)'}

Web search snippets (may be noisy — use only what clearly refers to THIS person/company):
{chr(10).join('- ' + s for s in snippets) if snippets else '(no results)'}

Return ONLY a JSON object:
{{"region":"NA|APAC|EMEA|UNKNOWN","country":"best-guess country or empty","location":"short location string or empty","confidence":"high|medium|low","reasoning":"one sentence citing the deciding signal"}}

If evidence is weak or conflicting, prefer UNKNOWN with low confidence rather than guessing APAC/EMEA — an UNKNOWN lead safely stays with the US rep."""
    try:
        out = claude_json(prompt)
        region = (out.get("region") or "UNKNOWN").upper()
        if region not in ("NA", "APAC", "EMEA", "UNKNOWN"):
            region = "UNKNOWN"
        return {
            "region": region, "route": ROUTE_BY_REGION.get(region, "hank"),
            "country": out.get("country", ""),
            "location": out.get("location", "") or out.get("country", ""),
            "linkedin_url": linkedin,
            "confidence": out.get("confidence", "low"),
            "reasoning": out.get("reasoning", ""),
            "signals": signals,
        }
    except Exception as e:
        log(f"Opus classify failed: {e}")
        return {
            "region": "UNKNOWN", "route": "hank", "country": "", "location": "",
            "linkedin_url": linkedin, "confidence": "low",
            "reasoning": "Research inconclusive; defaulting to Hank (US rep).",
            "signals": signals,
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--company", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    try:
        result = classify(args.email, args.name, args.company)
    except Exception as e:
        result = {"region": "UNKNOWN", "route": "hank", "country": "", "location": "",
                  "linkedin_url": "", "confidence": "low",
                  "reasoning": f"classifier error: {e}", "signals": {}}

    print(json.dumps(result, indent=2 if not args.json else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
