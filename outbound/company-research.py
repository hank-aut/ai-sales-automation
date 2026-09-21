import os
#!/usr/bin/env python3
"""
company-research.py — Research a company before outbound outreach.

Determines:
  1. What the company actually does
  2. Whether they're a good fit for DCH
  3. Correct persona classification
  4. Whether to proceed or skip

Uses Apollo org data + Hunter domain search. No fabrication.

Usage:
  python3 company-research.py "Company Name" "domain.com"
  python3 company-research.py "Company Name" "domain.com" --json
"""

import json
import re
import subprocess
import sys
import ssl
import urllib.request
import urllib.parse
from pathlib import Path

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

# What DCH actually offers — ground truth
DCH_VALUE = {
    "provider": "Competitive analysis, site selection, market share, pricing comps, vacancy/absorption tracking, planned capacity monitoring",
    "investor": "Due diligence, asset valuation, market sizing, transaction comps (2400+), capacity forecasts, hyperscaler tracking",
    "broker": "Vacancy identification, market reports, comp data, facility-level detail for client advisory",
    "vendor": "Identify where new builds are happening, track planned/under-construction facilities, find operators to sell to",
    "user": "Site selection, market comparison, capacity/pricing/availability across 80+ markets",
}

# Keywords that indicate each persona
PERSONA_SIGNALS = {
    "provider": ["colocation", "data center operator", "colo", "wholesale", "retail colocation",
                  "hyperscale", "data center developer", "builds data centers", "operates data centers",
                  "data center provider", "managed hosting", "data centre", "digital infrastructure provider",
                  "data center company", "data center campus"],
    "investor": ["investment", "private equity", "asset management", "fund", "capital",
                 "portfolio", "acquisition", "real estate investment", "infrastructure fund",
                 "hedge fund", "venture capital", "due diligence"],
    "broker": ["brokerage", "real estate advisory", "commercial real estate", "tenant rep",
               "site selection advisory", "data center consulting"],
    "vendor": ["construction", "electrical", "mechanical", "cooling", "power generation",
               "ups", "generator", "fiber", "cable", "engineering", "contractor",
               "equipment manufacturer", "critical infrastructure",
               "renewable energy", "hydropower", "solar power", "wind power",
               "energy company", "power company", "utility", "energy provider",
               "power systems", "electrical systems", "switchgear", "transformer"],
    "user": ["cloud", "ai compute", "gpu cloud", "machine learning", "enterprise it",
             "saas", "hyperscaler", "cloud provider", "neocloud"],
}

# Signals that make a company NOT a fit for DCH
NOT_A_FIT = [
    "staffing", "recruiting", "hr software", "marketing agency",
    "web design", "seo", "social media", "restaurant", "retail store",
    "real estate residential", "healthcare provider", "hospital",
]


def log(msg):
    if VERBOSE:
        print(f"  [research] {msg}", file=sys.stderr)


def apollo_org_lookup(domain):
    """Get org info from Apollo via people search (org data comes along)."""
    try:
        payload = json.dumps({
            "q_organization_domains_list": [domain],
            "page": 1,
            "per_page": 1,
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

        people = result.get("people", [])
        if people and people[0].get("organization"):
            org = people[0]["organization"]
            return {
                "name": org.get("name", ""),
                "industry": org.get("industry", ""),
                "description": org.get("short_description", ""),
                "keywords": org.get("keywords", []),
                "employees": org.get("estimated_num_employees", 0),
                "country": org.get("country", ""),
                "city": org.get("city", ""),
                "domain": org.get("primary_domain", ""),
            }
    except Exception as e:
        log(f"Apollo lookup error: {e}")
    return None


def apollo_org_enrich(domain):
    """Get org info via Apollo organization enrichment endpoint."""
    try:
        payload = json.dumps({"domain": domain}).encode()
        req = urllib.request.Request(
            "https://api.apollo.io/api/v1/organizations/enrich",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": APOLLO_API_KEY,
                "User-Agent": "Mozilla/5.0",
            },
        )
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
            result = json.loads(resp.read())
        org = result.get("organization", {})
        if org:
            return {
                "name": org.get("name", ""),
                "industry": org.get("industry", ""),
                "description": org.get("short_description", ""),
                "keywords": org.get("keywords", []),
                "employees": org.get("estimated_num_employees", 0),
                "country": org.get("country", ""),
                "city": org.get("city", ""),
                "domain": org.get("primary_domain", ""),
            }
    except Exception as e:
        log(f"Apollo org enrich error: {e}")
    return None


def hunter_org_lookup(domain):
    """Fallback: get org info from Hunter domain search."""
    HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")
    try:
        params = urllib.parse.urlencode({
            "domain": domain,
            "api_key": HUNTER_API_KEY,
            "limit": 5,
        })
        req = urllib.request.Request(f"https://api.hunter.io/v2/domain-search?{params}")
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as resp:
            result = json.loads(resp.read())
        data = result.get("data", {})
        org_name = data.get("organization", "")
        # Hunter gives us org name, pattern, and employee titles — use titles to infer industry
        titles = [e.get("position", "").lower() for e in data.get("emails", []) if e.get("position")]
        title_text = " ".join(titles)

        # Build a pseudo-description from titles
        description = f"{org_name}. Employee roles include: {', '.join(titles[:5])}" if titles else org_name

        return {
            "name": org_name,
            "industry": "",
            "description": description,
            "keywords": [],
            "employees": len(data.get("emails", [])),
            "country": data.get("country", ""),
            "city": "",
            "domain": domain,
            "source": "hunter",
        }
    except Exception as e:
        log(f"Hunter org lookup error: {e}")
    return None


def claude(prompt, model="claude-opus-4-8", max_tokens=600):
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }).encode(),
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=60) as resp:
        return json.loads(resp.read())["content"][0]["text"].strip()


def claude_json(prompt, model="claude-opus-4-8", max_tokens=600):
    """Opus first, fall back to Opus on error; parse the first JSON object."""
    try:
        text = claude(prompt, model=model, max_tokens=max_tokens)
    except Exception as e:
        log(f"{model} failed ({e}); retrying with claude-opus-4-8")
        text = claude(prompt, model="claude-opus-4-8", max_tokens=max_tokens)
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


VALID_PERSONAS = {"provider", "investor", "broker", "vendor", "user", "unknown"}


def classify_and_score(org_data, company_name):
    """Use Opus to classify persona AND score DCH fit from org data — judging what
    the company actually does, not keyword bingo. Returns (persona, score, reason).
    Falls back to the keyword rules only if the API is unreachable."""
    if not org_data:
        return "unknown", 5, "No org data available"
    desc = org_data.get("description", "") or ""
    industry = org_data.get("industry", "") or ""
    keywords = ", ".join((org_data.get("keywords") or [])[:15])
    employees = org_data.get("employees", 0)
    country = org_data.get("country", "") or ""
    prompt = (
        "You are qualifying a company as an OUTBOUND PROSPECT for datacenterHawk (DCH), a data center "
        "market-intelligence platform tracking data center supply, demand, pricing, and absorption across ~400 markets.\n\n"
        f"COMPANY: {company_name}\n"
        f"Apollo industry: {industry or 'unknown'}\n"
        f"Employees: {employees or 'unknown'}\n"
        f"HQ country: {country or 'unknown'}\n"
        f"Description: {desc or '(none)'}\n"
        f"Keywords: {keywords or '(none)'}\n\n"
        "Step 1 — PERSONA (how the company relates to the data center industry), exactly one of:\n"
        "  provider = builds/operates/leases data centers (colo, wholesale, hyperscale operator)\n"
        "  investor = PE / real estate / infrastructure capital investing in data centers\n"
        "  broker   = CRE brokerage / advisory / consulting for data centers\n"
        "  vendor   = sells INTO data centers (power, cooling, construction, equipment, fiber, energy)\n"
        "  user     = consumes data center capacity as a tenant (cloud, AI, streaming, large enterprise IT)\n"
        "  unknown  = cannot tell from the data\n\n"
        "Step 2 — FIT score 1-10: how useful DCH's market intelligence is to THIS company and how likely they buy. "
        "8-10 = clearly in the DC ecosystem and would rely on supply/pricing/absorption data; "
        "5-7 = plausibly relevant, adjacent, or a large capacity-consuming tenant; "
        "1-4 = little/no data center relevance or clearly not a buyer (consumer retail, staffing, healthcare, etc.).\n\n"
        "Judge by what the company ACTUALLY does. Do NOT be fooled by generic corporate boilerplate — words like "
        "'capital', 'portfolio', 'infrastructure', 'global' do not by themselves make a company an investor or a fit. "
        "A large media/streaming company is a 'user' (data center tenant), not an 'investor'.\n\n"
        'Return ONLY JSON: {"persona": "...", "fit_score": N, "fit_reason": "one concrete sentence on why"}'
    )
    try:
        data = claude_json(prompt)
        persona = (data.get("persona") or "unknown").strip().lower()
        if persona not in VALID_PERSONAS:
            persona = "unknown"
        score = max(1, min(10, int(data.get("fit_score") or 5)))
        reason = (data.get("fit_reason") or "").strip() or "Opus classification"
        return persona, score, reason
    except Exception as e:
        log(f"Opus classify failed ({e}); falling back to keyword rules")
        persona = classify_persona(org_data)
        score, reason = score_fit(org_data, persona)
        return persona, score, f"{reason} (keyword fallback)"


def classify_persona(org_data):
    """Determine persona from org data. Description is weighted 3x over keywords."""
    if not org_data:
        return "unknown"

    description = (org_data.get("description", "") + " " + org_data.get("industry", "")).lower()
    # Only use first 10 keywords — Apollo dumps hundreds of generic tags
    keywords = " ".join(org_data.get("keywords", [])[:10]).lower()

    scores = {}
    for persona, signals in PERSONA_SIGNALS.items():
        # Description hits count 3x
        desc_hits = sum(3 for s in signals if s in description)
        # Keyword hits count 1x
        kw_hits = sum(1 for s in signals if s in keywords)
        scores[persona] = desc_hits + kw_hits

    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "unknown"
    return best


def score_fit(org_data, persona):
    """Score 1-10 whether this company is a good fit for DCH."""
    if not org_data:
        return 5, "No org data available"

    score = 5  # Baseline
    reasons = []

    # Use description only for NOT_A_FIT check (keywords are too noisy)
    description_only = org_data.get("description", "").lower()
    # Full text for DC relevance scoring (description + first 10 keywords)
    desc = (org_data.get("description", "") + " " + " ".join(org_data.get("keywords", [])[:10])).lower()

    # Check for NOT a fit signals — description only, not keywords
    for signal in NOT_A_FIT:
        if signal in description_only:
            return 1, f"Not a fit: '{signal}' detected in description"

    # Data center relevance
    dc_terms = ["data center", "data centre", "colocation", "colo ", "hyperscale",
                "digital infrastructure", "critical infrastructure"]
    dc_hits = sum(1 for t in dc_terms if t in desc)
    if dc_hits >= 2:
        score += 3
        reasons.append(f"Strong DC relevance ({dc_hits} signals)")
    elif dc_hits == 1:
        score += 1
        reasons.append("Some DC relevance")
    else:
        score -= 2
        reasons.append("No direct DC mention")

    # Company size
    employees = org_data.get("employees", 0)
    if employees >= 50:
        score += 1
        reasons.append(f"{employees} employees")
    elif employees < 10:
        score -= 1
        reasons.append(f"Very small ({employees} emp)")

    # Persona-specific boosts
    if persona == "investor" and any(w in desc for w in ["infrastructure", "real estate", "data center"]):
        score += 1
        reasons.append("Investor with infra focus")
    if persona == "vendor" and any(w in desc for w in ["construction", "power", "cooling", "electrical"]):
        score += 1
        reasons.append("Vendor in DC supply chain")

    # Geographic
    country = org_data.get("country", "").lower()
    if country in ["united states", "canada", "united kingdom", "germany", "france", "netherlands"]:
        score += 1
        reasons.append(f"Good geo: {org_data.get('country')}")

    score = max(1, min(10, score))
    return score, "; ".join(reasons) if reasons else "Baseline score"


def research_company(company_name, domain):
    """Full research on a company. Returns research dict."""
    result = {
        "company": company_name,
        "domain": domain,
        "org_data": None,
        "persona": "unknown",
        "fit_score": 0,
        "fit_reason": "",
        "recommendation": "",
        "description": "",
    }

    # Apollo lookup — org enrich first (reliable), then people search, then Hunter fallback
    log(f"Looking up {domain} on Apollo...")
    org = apollo_org_enrich(domain)
    if not org:
        log("Org enrich returned nothing, trying people search...")
        org = apollo_org_lookup(domain)
    if not org:
        log("Apollo failed, falling back to Hunter domain search...")
        org = hunter_org_lookup(domain)
    result["org_data"] = org

    if org:
        result["description"] = org.get("description", "")[:300]
        log(f"  Industry: {org.get('industry')}")
        log(f"  Employees: {org.get('employees')}")
        log(f"  Description: {org.get('description', '')[:100]}")

        # Classify persona + score fit with Opus (judges what the company does,
        # not keyword matches). Keyword rules remain as an API-failure fallback.
        persona, score, reason = classify_and_score(org, company_name)
        result["persona"] = persona
        result["fit_score"] = score
        result["fit_reason"] = reason
        log(f"  Persona: {persona}")
        log(f"  Fit score: {score}/10 ({reason})")

        # Recommendation
        if score >= 7:
            result["recommendation"] = "PROCEED"
        elif score >= 5:
            result["recommendation"] = "REVIEW"
        else:
            result["recommendation"] = "SKIP"
    else:
        result["fit_score"] = 5
        result["fit_reason"] = "No Apollo data — manual review needed"
        result["recommendation"] = "REVIEW"

    return result


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if len(args) < 2:
        print("Usage: company-research.py 'Company Name' 'domain.com'")
        sys.exit(1)

    company = args[0]
    domain = args[1]

    result = research_company(company, domain)

    if "--json" in sys.argv:
        print(json.dumps(result, indent=2, default=str))
    else:
        icon = {"PROCEED": "✅", "REVIEW": "⚠️", "SKIP": "❌"}.get(result["recommendation"], "?")
        print(f"\n{icon}  {company} — {result['recommendation']}")
        print(f"   Persona: {result['persona']}")
        print(f"   Fit score: {result['fit_score']}/10")
        print(f"   Reason: {result['fit_reason']}")
        if result["description"]:
            print(f"   Description: {result['description'][:200]}")


if __name__ == "__main__":
    main()
