# AI Sales Automation Engine

An autonomous, **human-in-the-loop** B2B prospecting system that runs a full sales
pipeline end to end: it detects buying signals, decides who's fair game, researches
the account, finds and verifies contacts, drafts outreach in the rep's own voice, and
posts an approve/edit/skip card to Slack — then sends and schedules the follow-up
sequence once a human taps **Approve**.

I built this as a working Account Executive to run my *own* pipeline. It self-sources
net-new opportunities and logs everything back to the CRM, which let me carry the
highest production on my team while doing a fraction of the manual prospecting.

> **Note:** This is a sanitized, personal copy of a production system. All credentials
> load from environment variables (see [`.env.example`](.env.example)); no keys,
> customer data, or internal identifiers are included. LLM calls target Anthropic's
> Claude models.

---

## Why it exists

Manual prospecting is where reps lose their week: chasing signals, checking whether an
account is already being worked, enriching contacts, verifying emails, and writing
outreach that doesn't sound like a robot. Each of those is a discrete, automatable
decision. This engine chains them into one pipeline and keeps a human at the single
point that matters — the moment before an email goes out.

## Architecture

```
                          ┌──────────────────────────────────────────┐
   SIGNALS                │              OUTBOUND FLOW                 │
   ┌──────────────┐       │                                           │
   │ market events│──┐    │  1. signal scan ──► 2. ROE check (gate)    │
   │ power/build  │  │    │        │                   │ CLEAR         │
   │ conferences  │──┼───►│        ▼                   ▼               │
   │ news feeds   │  │    │  3. company research ──► 4. contact finder │
   └──────────────┘  │    │        (LLM fit score)      (verify email) │
                     │    │                             │              │
                     │    │                             ▼              │
                     │    │  5. draft outreach (voice-matched, LLM)    │
                     │    │        │                                   │
                     │    │        ▼                                   │
                     │    │  6. Slack card ──[human]──► 7. send + CRM  │
                     │    │     approve/edit/skip          + sequence  │
                     │    └──────────────────────────────────────────┘
   INBOUND
   ┌──────────────┐       ┌──────────────────────────────────────────┐
   │ inbound lead │──────►│  ROE check ──► draft reply ──► Slack card  │
   │ (form / web) │       │  ──[human approve]──► send + auto sequence │
   └──────────────┘       └──────────────────────────────────────────┘
```

Everything funnels through two gates: the **Rules-of-Engagement (ROE) engine**
(is this account fair to work?) and a **human approval card in Slack** (should this
specific email actually go out?). Nothing sends cold without both.

## The interesting engineering

- **Rules-of-Engagement engine** (`outbound/roe-check.py`) — the heart of the system.
  Decides whether an account is workable based on *activity*, not ownership: recent
  Tasks/Events/Opportunities, in-flight outreach, current-customer status, and
  same-domain leads. Handles the ugly real-world edges — M&A subsidiaries, freemail
  domains, parent/`.ca`/`.net` domain drift, bounces that aren't real engagement.
- **Voice-matched drafting** — outreach is generated against a vector store of the
  rep's real sent emails, so drafts sound like a person, not a template. A shared
  AI-tell scrubber (`shared/email_safety.py`) strips the giveaways (em-dashes,
  contrast clichés, corporate filler) before anything is shown.
- **Human-in-the-loop by design** — every send is gated behind a Slack Block Kit card
  with Approve / Edit / Ask-to-edit / Skip. The listener turns a button tap into a
  real send + CRM write + follow-up scheduling.
- **Multi-source signal detection** (`signals/`, `outbound/outbound-daily.py`) — new
  pipeline is seeded from market events, power/early-build signals, conference
  attendee lists, and news, each scored for fit by an LLM rather than keyword rules.
- **Sequenced follow-ups** — once Email 1 is approved, Emails 2/3 auto-fire on a
  schedule, gated by a reply-check and a fresh ROE pass so you never follow up into a
  reply or a newly-off-limits account.

## Repo layout

| Path | What it does |
|---|---|
| `outbound/outbound-pipeline.py` | Orchestrator: signal → ROE → research → contact → draft |
| `outbound/roe-check.py` | Rules-of-Engagement engine (account workability) |
| `outbound/company-research.py` | LLM company research + fit scoring |
| `outbound/contact-finder.py` | Contact discovery + multi-provider email verification |
| `outbound/outbound-daily.py` | Daily multi-source signal scan → prospect queue |
| `outbound/outbound-post-card-slack.py` | Posts the interactive approval card |
| `outbound/outbound-action-slack.py` | Handles button taps: send + CRM + sequence |
| `outbound/outbound-sequence.py` | Auto follow-up Email 2/3 engine |
| `inbound/inbound-action-slack.py` | Inbound lead → approval card → send |
| `inbound/inbound-sequence.py` | Inbound follow-up sequence engine |
| `shared/email_safety.py` | AI-tell / tone scrubber shared across flows |
| `signals/mkt-passive-poll.py` | Passive market-event poller → auto-cards |

## Tech stack

Python · Anthropic Claude (research, fit scoring, drafting) · Salesforce (CRM of
record) · Slack Block Kit (human-in-the-loop approval) · Apollo / Hunter / Brave
(enrichment + email verification) · Microsoft Graph (send) · vector search over sent
email for voice matching.

## Setup

```bash
cp .env.example .env      # fill in your own API keys
pip install -r requirements.txt
```

Each script is standalone and reads its config from the environment. See
`.env.example` for the full list of variables.

## Disclaimer

Personal project. Sanitized for public release — no live credentials, customer data,
or internal infrastructure identifiers are present. The logic reflects patterns I
built and ran; adapt the CRM/enrichment integrations to your own stack.
