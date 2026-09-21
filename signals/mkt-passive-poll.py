import os
#!/usr/bin/env python3
"""
mkt-passive-poll.py — Daily poll of #mkt-events-passive for outbound-worthy companies.

Logic (per Hank, 2026-06-19):
  1. Pull new passive-channel leads since last run.
  2. Keep only real corporate domains (drop free/personal/academic/gov + non-NA/EU TLDs).
  3. LEAD-STEALING GUARDRAIL: if the same person/domain showed up in #mkt-events
     (non-passive, = routed to a rep) in the last 7 days:
        - routed < 4 days ago  -> SKIP (rep may just not have worked it yet — their lead)
        - routed >= 4 days ago with no outreach -> allow (free to hit)
  4. CORRECTED ROE (real sales activity only): block ONLY on Tasks w/ TaskSubtype in
     (Email,Call), real Events, or open/recent Opps in last 120d. Marketplace-submission
     tasks (TaskSubtype='Task') and dormant marketing leads do NOT block.
     Current/past customers are flagged separately (don't cold-outbound customers).
  5. For each CLEAR survivor (2026-06-26, "B"): resolve company+country from the
     domain (Apollo), geo-gate (NA/EU only), run it through outbound-pipeline.py,
     and if READY auto-post an interactive APPROVE/EDIT/SKIP card to #rudy-outbound
     via outbound-post-card-slack.py. Daily-capped (MKT_CARD_CAP, default 5).
     Posts an outcome digest (CARD POSTED / low-fit / no-contact / geo / cap).
     Nothing sends — Hank still taps a button. Re-runs are safe: the pipeline's
     30-day cooldown + the card poster's idempotency prevent double-cards.

Run daily via launchd (see com.dch.mkt-passive-poll.plist). Requires:
  export SF_USE_GENERIC_UNIX_KEYCHAIN=true   (set in plist EnvironmentVariables)
  DRY_RUN=1   -> resolve+digest only, no pipeline run / no cards
  MKT_CARD_CAP=N -> override the daily card cap (default 5)
"""
import json, os, re, ssl, subprocess, sys, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

ORG = "you@example.com"
PASSIVE = "C0000000000"
NONPASSIVE = "C0000000000"
RUDY_OUTBOUND = "C0000000000"
STATE = Path.home() / ".openclaw/workspace/mkt-passive-poll-state.json"
LOOKBACK_DAYS = 2          # how far back to scan passive each run (overlap is fine; state dedups)
ROE_CUTOFF_DAYS = 120
GUARDRAIL_DAYS = 7         # window to check non-passive routing
GUARDRAIL_FRESH_DAYS = 4   # < this many days routed = still the rep's lead

USER_TOKEN = os.environ.get("SLACK_USER_TOKEN", "")  # xoxp, read history + post
try:
    SSL_CTX = __import__("ssl").create_default_context(cafile=__import__("certifi").where())
except Exception:
    SSL_CTX = ssl.create_default_context()

FREE = {"gmail.com","yahoo.com","hotmail.com","outlook.com","icloud.com","aol.com","proton.me",
"protonmail.com","gmx.com","gmx.de","gmx.net","live.com","msn.com","me.com","mac.com","comcast.net",
"ymail.com","qq.com","163.com","126.com","yandex.com","mail.com","googlemail.com","sbcglobal.net",
"att.net","verizon.net","cox.net","mail.ru","zoho.com","pm.me","fastmail.com","duck.com","inbox.eu",
"email.com","myemail.com","maildrop.cc","poczta.fm","libero.it","web.de","naver.com","hanmail.net",
"btinternet.com","bellsouth.net","charter.net","compuserve.com","rediffmail.com","hotwiremail.com",
"passmail.net","outlook.de","hotmail.it","yahoo.it","hotmail.es"}
APAC_TLD = (".my",".cn",".in",".jp",".kr",".id",".sg",".hk",".tw",".th",".ph",".vn",".pk",".bd",
".lk",".com.au",".co.nz",".com.cn",".co.in",".co.id",".com.my",".com.tw",".com.hk",".com.sg",
".co.kr",".co.th",".ac.in",".ac.kr",".ac.th",".com.np")

def is_corporate(dom):
    if not dom: return False
    dom = dom.lower()
    if dom in FREE: return False
    if dom.endswith((".edu",".ac.uk",".edu.au",".gov",".mil")) or ".edu." in dom or ".gov." in dom: return False
    if any(dom.endswith(t) for t in APAC_TLD): return False
    return True

EMAIL_RE = re.compile(r'mailto:([^|>\s]+@[^|>\s]+)')

def slack_history(channel, oldest):
    msgs=[]; cursor=None
    while True:
        p={"channel":channel,"limit":"200","oldest":f"{oldest:.6f}"}
        if cursor: p["cursor"]=cursor
        url="https://slack.com/api/conversations.history?"+urllib.parse.urlencode(p)
        req=urllib.request.Request(url,headers={"Authorization":"Bearer "+USER_TOKEN})
        d=json.loads(urllib.request.urlopen(req,context=SSL_CTX).read().decode())
        if not d.get("ok"): print("slack err",channel,d.get("error")); break
        msgs+=d.get("messages",[])
        cursor=d.get("response_metadata",{}).get("next_cursor")
        if not cursor: break
    return msgs

def parse_leads(msgs):
    out=[]
    for m in msgs:
        txt=m.get("text","") or ""
        em=EMAIL_RE.search(txt)
        if not em: continue
        email=em.group(1).strip().lower()
        dom=email.split("@")[1] if "@" in email else None
        nm=re.search(r'mortar_board:?\s*\**\s*([^(]+?)\s*\(<?mailto', txt)
        out.append({"ts":float(m.get("ts","0")),"email":email,"domain":dom,
                    "name":nm.group(1).strip() if nm else None})
    return out

def sf(soql):
    r=subprocess.run(["sf","data","query","-o",ORG,"--json","--query",soql],capture_output=True,text=True)
    try: return json.loads(r.stdout)["result"]["records"]
    except Exception: return []

def corrected_roe(domain, name=None):
    """Return (status, note). status in {CLEAR, BLOCKED, CUSTOMER}."""
    cut=(datetime.now(timezone.utc)-timedelta(days=ROE_CUTOFF_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    d=domain.split("@")[-1]
    accs=sf(f"SELECT Id,Name,Type,Customer_Designation__c FROM Account WHERE Website LIKE '%{d}%'"+
            (f" OR Name='{name.replace(chr(39),chr(92)+chr(39))}'" if name else "")+" LIMIT 10")
    for a in accs:
        cd=(a.get("Customer_Designation__c") or ""); ty=(a.get("Type") or "")
        if "Customer" in cd or "Customer" in ty:
            return "CUSTOMER", f"{a['Name']} [{cd or ty}]"
    for a in accs:
        aid=a["Id"]
        t=sf(f"SELECT COUNT(Id) c FROM Task WHERE (AccountId='{aid}' OR WhatId='{aid}') AND TaskSubtype IN ('Email','Call') AND CreatedDate>={cut}")
        e=sf(f"SELECT COUNT(Id) c FROM Event WHERE (AccountId='{aid}' OR WhatId='{aid}') AND CreatedDate>={cut}")
        o=sf(f"SELECT COUNT(Id) c FROM Opportunity WHERE AccountId='{aid}' AND (IsClosed=false OR LastModifiedDate>={cut})")
        if (t[0]['c'] if t else 0)+(e[0]['c'] if e else 0)+(o[0]['c'] if o else 0)>0:
            return "BLOCKED", f"real sales activity on {a['Name']}"
    leads=sf(f"SELECT Id FROM Lead WHERE Email LIKE '%@{d}' LIMIT 10")
    for l in leads:
        lt=sf(f"SELECT COUNT(Id) c FROM Task WHERE WhoId='{l['Id']}' AND TaskSubtype IN ('Email','Call') AND CreatedDate>={cut}")
        if (lt[0]['c'] if lt else 0)>0:
            return "BLOCKED", "real sales activity on lead"
    note="net-new" if not (accs or leads) else f"{len(accs)}acct/{len(leads)}lead, no real activity"
    return "CLEAR", note

def guardrail(domain, nonpassive_leads):
    """Return (ok, note). Blocks if domain routed to a rep < GUARDRAIL_FRESH_DAYS ago."""
    now=datetime.now(timezone.utc).timestamp()
    hits=[l for l in nonpassive_leads if l["domain"]==domain and (now-l["ts"])/86400 <= GUARDRAIL_DAYS]
    if not hits: return True, ""
    newest=max(h["ts"] for h in hits); age=(now-newest)/86400
    if age < GUARDRAIL_FRESH_DAYS:
        return False, f"routed to rep {age:.1f}d ago (non-passive) — likely still their lead"
    return True, f"appeared non-passive {age:.1f}d ago, no outreach — free to hit"

# --------------------------------------------------------------------------- #
# B (2026-06-26): auto-run CLEAR candidates through the pipeline + post cards.
#   Previously this script only posted a text digest; the bridge to interactive
#   cards never existed (outbound-draft-poster only cards companies already in
#   pipeline-results.json). Now each CLEAR candidate is run through
#   outbound-pipeline.py and, if READY, carded via outbound-post-card-slack.py.
#   Geo-gated (NA/EU only) + daily-capped so #rudy-outbound can't be flooded.
# --------------------------------------------------------------------------- #
import importlib.util
PIPELINE   = str(Path.home() / ".local/bin/outbound-pipeline.py")
POST_CARD  = str(Path.home() / ".local/bin/outbound-post-card-slack.py")
RESULTS    = Path.home() / ".openclaw/workspace/outbound-v2/pipeline-results.json"
CARD_CAP   = int(os.environ.get("MKT_CARD_CAP", "5"))   # max cards/day from this feed
PIPE_TIMEOUT = 240

# NA + EU allowlist. A candidate resolving to a country OUTSIDE this set is skipped
# (catches APAC .com domains like korindo-energy.com that pass the TLD filter).
# Unknown/empty country is allowed — we don't over-block on missing Apollo data.
NA_EU = {"united states","usa","us","canada","mexico","united kingdom","uk","england",
"scotland","wales","ireland","germany","france","netherlands","spain","italy","portugal",
"belgium","luxembourg","switzerland","austria","sweden","norway","denmark","finland",
"iceland","poland","czechia","czech republic","slovakia","hungary","romania","bulgaria",
"greece","croatia","slovenia","estonia","latvia","lithuania","serbia","ukraine"}

def _load_cf():
    try:
        spec=importlib.util.spec_from_file_location("contact_finder",
            str(Path.home() / ".local/bin/contact-finder.py"))
        mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
    except Exception as e:
        print("contact-finder import failed:",e); return None
_CF=_load_cf()

def resolve_company(domain):
    """(company_name, country) from a domain via Apollo people-by-domain.
    Falls back to a titlecased domain SLD + unknown country."""
    name,country=None,""
    if _CF:
        try:
            for p in (_CF.apollo_search(domain, per_page=5) or []):
                org=p.get("organization") or {}
                if not name and org.get("name"): name=org.get("name")
                country=country or org.get("country") or p.get("country") or ""
                if name and country: break
        except Exception as e:
            print(f"  apollo resolve error {domain}: {e}")
    if not name:
        name=domain.split(".")[0].replace("-"," ").title()
    return name,(country or "").strip()

def geo_ok(country):
    if not country: return True            # unknown -> allow
    return country.lower() in NA_EU

def run_pipeline(company, domain, signal):
    try:
        subprocess.run(["python3",PIPELINE,company,domain,"--signal",signal],
                       capture_output=True,text=True,timeout=PIPE_TIMEOUT)
    except Exception as e:
        print(f"  pipeline error {company}: {e}")

def read_result(company):
    try:
        return (json.loads(RESULTS.read_text()) or {}).get(company.lower().strip()) or {}
    except Exception:
        return {}

def post_card(company):
    try:
        r=subprocess.run(["python3",POST_CARD,company],capture_output=True,text=True,timeout=90)
        return ((r.stdout or "")+(r.stderr or "")).strip()
    except Exception as e:
        return f"error: {e}"

def post_digest(lines):
    text="*:satellite: mkt-events-passive — outbound run*\n"+"\n".join(lines)
    data=urllib.parse.urlencode({"channel":RUDY_OUTBOUND,"text":text,"unfurl_links":"false"}).encode()
    req=urllib.request.Request("https://slack.com/api/chat.postMessage",data=data,
        headers={"Authorization":"Bearer "+USER_TOKEN})
    r=json.loads(urllib.request.urlopen(req,context=SSL_CTX).read().decode())
    print("posted digest ok=",r.get("ok"),r.get("error",""))

def main():
    state=json.loads(STATE.read_text()) if STATE.exists() else {"seen_domains":[]}
    seen=set(state.get("seen_domains",[]))
    now=datetime.now(timezone.utc).timestamp()
    oldest=now-LOOKBACK_DAYS*86400
    passive=parse_leads(slack_history(PASSIVE,oldest))
    nonpassive=parse_leads(slack_history(NONPASSIVE,now-GUARDRAIL_DAYS*86400))
    # unique new corporate domains
    cands={}
    for l in passive:
        if not is_corporate(l["domain"]): continue
        if l["domain"] in seen: continue
        cands.setdefault(l["domain"],l)
    print(f"new corporate passive domains: {len(cands)}")

    # Daily card-cap accounting (resets each UTC day).
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("card_day")!=today:
        state["card_day"]=today; state["cards_today"]=0
    cards_today=state.get("cards_today",0)
    dry=bool(os.environ.get("DRY_RUN"))

    digest=[]
    for dom,l in cands.items():
        ok,gnote=guardrail(dom,nonpassive)
        if not ok:
            print(f"SKIP {dom}: {gnote}"); seen.add(dom); continue
        status,rnote=corrected_roe(dom,None)
        if status!="CLEAR":
            print(f"skip {dom}: {status} {rnote}"); seen.add(dom); continue

        # Resolve company + geo gate BEFORE spending pipeline credits.
        company,country=resolve_company(dom)
        if not geo_ok(country):
            digest.append(f"• *{dom}* — {company} — :no_entry: SKIPPED (geo: {country}, NA/EU only)")
            print(f"skip {dom}: geo {country}"); seen.add(dom); continue

        who=f" ({l['name']})" if l.get("name") else ""
        if cards_today>=CARD_CAP:
            digest.append(f"• *{dom}* — {company}{who} — CLEAR ({rnote}) — not carded (daily cap {CARD_CAP} hit)")
            seen.add(dom); continue

        # fit-only signal: tells Opus WHY this surfaced (so a good fit isn't SKIPPED on a
        # stale Apollo blurb) but instructs the drafter not to reference the form submission.
        signal=(f"{company} surfaced via inbound interest on a data center marketplace listing — "
                f"treat as a net-new prospect in the DC ecosystem. Do NOT reference the form/"
                f"submission in the email; open on their data-center relevance instead.")
        if dry:
            digest.append(f"• *{dom}* — {company}{who} — CLEAR ({rnote}) — [DRY_RUN: would pipeline+card]")
            seen.add(dom); continue

        run_pipeline(company,dom,signal)
        res=read_result(company); st=(res.get("status") or "").upper()
        rcountry=((res.get("research") or {}).get("country") or "").strip()
        fit=((res.get("research") or {}).get("fit_score","?"))
        if rcountry and not geo_ok(rcountry):
            digest.append(f"• *{dom}* — {company} — :no_entry: SKIPPED (geo: {rcountry}, NA/EU only)")
        elif st=="READY":
            out=post_card(company)
            if "ALREADY_POSTED" in out:
                digest.append(f"• *{dom}* — {company} — already carded")
            elif out and "error" not in out.lower() and "false" not in out.lower():
                cards_today+=1
                c=((res.get("contact") or {}).get("best_contact") or {})
                cinfo=f" → {c.get('name','?')} <{c.get('email','?')}>" if c else ""
                digest.append(f"• *{dom}* — {company} — :white_check_mark: CARD POSTED (fit {fit}/10){cinfo}")
            else:
                digest.append(f"• *{dom}* — {company} — READY but card post failed ({out[:80]})")
        elif "BLOCKED" in st:
            digest.append(f"• *{dom}* — {company} — BLOCKED by pipeline ROE")
        elif "NO_CONTACT" in st:
            digest.append(f"• *{dom}* — {company} — CLEAR but no verified contact (manual)")
        elif "SKIPPED" in st or "NOT A FIT" in st:
            digest.append(f"• *{dom}* — {company} — low fit ({fit}/10), no card")
        elif "ALREADY_PROCESSED" in st:
            digest.append(f"• *{dom}* — {company} — already processed recently")
        elif "EXCLUDED" in st:
            digest.append(f"• *{dom}* — {company} — excluded")
        else:
            digest.append(f"• *{dom}* — {company} — {st or 'no result'}")
        seen.add(dom)

    state["cards_today"]=cards_today
    if digest:
        if os.environ.get("DRY_RUN"):
            print("[DRY_RUN] would post digest:\n"+"\n".join(digest))
        else:
            post_digest(digest)
    else:
        print("no new clear candidates today")
    state["seen_domains"]=sorted(seen)
    state["last_run"]=datetime.now(timezone.utc).isoformat()
    STATE.write_text(json.dumps(state,indent=2))

if __name__=="__main__":
    main()
