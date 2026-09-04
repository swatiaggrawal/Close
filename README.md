# Close — Voice Sales Agent for Talentbridge Consulting

**Built on Agora's Conversational AI Engine.** A real-time voice AI sales and
qualification agent for a staffing/recruitment consultancy demo scenario.
Agora's own managed LLM (`gpt-4o-mini`) handles all conversation reasoning,
memory, turn-taking, and interruption handling natively — this repo only
exposes plain REST endpoints that Agora calls directly via **Custom Tools**.
No custom LLM adapter, no second model hop, no orchestration layer of our
own — Agora *is* the agent runtime.

---

## Table of contents

- [Architecture](#architecture)
- [What's real vs. simulated](#whats-real-vs-simulated)
- [Tools](#tools)
- [System prompt](#system-prompt)
- [CRM (Google Sheets)](#crm-google-sheets)
- [Automated tests](#automated-tests)
- [Manual voice evals](#manual-voice-evals)
- [Running locally](#running-locally)
- [Deployment on Render](#deployment-on-render)
- [Dashboard & logs](#dashboard--logs)
- [Known limitations / issues found during testing](#known-limitations--issues-found-during-testing)

---

## Architecture

Two real pivots happened during development — both documented here rather
than hidden, because the reasoning is part of the engineering story.

**1. Custom LLM adapter (Gemini via LangChain) → Agora-native tool-calling.**
We initially built a FastAPI adapter exposing an OpenAI-compatible endpoint
in front of Gemini, to get real tool-calling since Agora's native Gemini
config didn't expose function-calling. It worked, but added a second LLM hop
(~12s latency per turn) on top of Agora's own reasoning — too slow for
real-time voice. Switched to Agora's native Custom Tools calling this
service directly: no adapter, no double LLM hop.

**2. Standalone `book_meeting` tool → merged into `create_lead`.**
We built and tested a dedicated booking tool with real Google Calendar
integration. It worked reliably when tested in isolation via direct HTTP
calls to this backend, but showed a platform-level dispatch issue through
Agora — the LLM would narrate "let me try that again" but the tool call
sometimes never reached this service at all (confirmed via server-side logs
showing zero incoming requests during failures, despite direct curl calls to
the same endpoint always succeeding). Rather than ship an unreliable
feature, we consolidated meeting requests into `create_lead` — the most
reliable tool in the system — as an optional field. A meeting request now
travels through the same dependable path as lead capture, and still creates
a real Calendar event when a time is provided. `book_meeting`'s code is kept
in this repo for reference but is **no longer registered as an active Agora
Custom Tool.**

```
Client (voice) ──▶ Agora Conversational AI Engine (gpt-4o-mini)
                        │  reasoning, memory, turn-taking, interruption handling
                        ▼
                 Custom Tools (HTTP, Studio v2)
                        │
                        ▼
        FastAPI backend (this repo) ──▶ Google Sheets (CRM)
                        │            ──▶ Google Calendar
                        │            ──▶ Resend (email)
                        └──▶ local JSONL logs + /dashboard
```

---

## What's real vs. simulated

**Real:**
- Voice pipeline, conversation reasoning, memory, turn-taking, interruption
  handling — all Agora-native (`gpt-4o-mini`)
- Pricing calculation — real tiered/volume-discount logic across 5 service
  categories (finance, IT, executive, marketing, media)
- Service info retrieval — keyword-scored search over a small in-memory
  corpus (see "RAG" note below)
- **Google Calendar booking** — real events created via a Google Calendar
  API service account, triggered through `create_lead`'s optional
  `preferred_meeting_time` field
- **Google Sheets CRM** — real rows appended to a live spreadsheet on every
  lead capture
- **Real email notifications** — both internal (to the recruitment team) and
  client-facing confirmations, sent via Resend, for lead capture and
  escalation
- Full tool-call and error logging to disk, plus a live `/dashboard` view

**Simulated / not fully solved:**
- The standalone `book_meeting` endpoint code still exists and works
  correctly in isolation, but is not currently wired to Agora due to the
  platform reliability issue described above
- Client email/phone capture depends on ASR accuracy for spelled-out
  strings — mitigated with a confirm-before-proceeding step in the prompt
  and server-side normalization/validation, but not fully solvable (a
  mistranscribed word that happens to look like a valid email locally can't
  be caught by validation alone)

**On "RAG":** `search_service_info` uses keyword-overlap scoring, not
embeddings-based retrieval. It correctly grounds answers in real documents
rather than letting the model invent facts, which is the functional goal —
but it's precise to call it "retrieval-based," not full RAG, if asked to
elaborate on the technique.

---

## Tools

Registered as **Agora Studio v2 Custom HTTP Tools**, each attached to the
saved agent, each backed by an endpoint on this FastAPI service.

### Active (4)

| Tool | Endpoint | What it does | Real integrations |
|---|---|---|---|
| `get_staffing_quote` | `POST /tools/get_staffing_quote` | Tiered pricing with volume discounts across finance, IT, executive, marketing, media | Pure computation, always reliable |
| `search_service_info` | `POST /tools/search_service_info` | Answers service/guarantee/competitor questions | Keyword-scored retrieval over 7 docs |
| `create_lead` | `POST /tools/create_lead` | Captures lead info, **and optionally books a meeting** if `preferred_meeting_time` is given | Google Sheets row, Google Calendar event, internal + client emails |
| `escalate_to_human` | `POST /tools/escalate_to_human` | Hands off to a human with full context, once per call | Internal email with conversation summary, client confirmation email |

**`create_lead` — parameters**

| Field | Type | Required | Description |
|---|---|---|---|
| `customer_name` | string | ✅ | Client's name |
| `email` | string | — | Ask for this before the call ends if not already captured |
| `requirements_summary` | string | — | Current, up-to-date summary of what the client needs |
| `preferred_meeting_time` | string | — | Client's stated preferred day/time, if they've asked to meet. Leave empty otherwise |

Call condition (enforced by the prompt, not the schema): as soon as the
agent knows the client's **name + at least one concrete requirement**
(role type + headcount). Called again whenever requirements materially
change, or when a meeting time is added after the first call.

**`escalate_to_human` — parameters**

| Field | Type | Required |
|---|---|---|
| `reason` | string | ✅ |
| `conversation_summary` | string | ✅ |
| `customer_name` | string | — (defaults `"Not provided"`) |
| `contact_info` | string | — (defaults `"Not provided"`) |

### Deprecated (1) — kept in code, not registered in Agora

| Tool | Endpoint | Why it's deprecated |
|---|---|---|
| `book_meeting` | `POST /tools/book_meeting` | Reliable in isolated HTTP testing, but showed a platform-level Agora dispatch issue in practice — see [Architecture](#architecture). Superseded by `create_lead`'s `preferred_meeting_time` field. |

`GET /health` reflects this split directly — `active_tools` lists the 4
live tools, `deprecated_endpoints_still_present` flags `book_meeting`.

---

## System prompt

This is the agent's instruction prompt as configured in Agora Studio (System
Message). It's the corrected version, after fixing two bugs found during
testing: a phantom `schedule_recruitment_call` tool reference left over from
before the `book_meeting` → `create_lead` merge, and a duplicated,
conflicting `USING create_lead` section.

```
You are Close, a voice sales and qualification agent working on behalf of Talentbridge Consulting, a staffing and recruitment consultancy.

YOUR JOB
A client is calling about a hiring need. Your job across the call is to:
1. Understand what they actually need (role type, how many hires, timeline).
2. Keep that understanding current -- if they change a number or add a new requirement mid-call, treat the NEW value as correct and carry it forward for the rest of the conversation. Never fall back to an earlier number once it's been updated.
3. Answer pricing, service, and availability questions using the tools provided -- never invent numbers or capabilities. If a tool doesn't cover something, say you'll confirm it rather than guessing.
4. Handle objections the way a good salesperson would, not by refusing to engage: pricing pushback gets reframed around value and volume pricing, competitor comparisons get a factual, confident answer, trust or hesitation gets addressed directly and can be offered a human follow-up.
5. Qualify the lead conversationally -- you're listening for role type, headcount, timeline, and decision-making authority, not interrogating a form.
6. Always be steering toward a concrete next step: booking a meeting with the recruitment team is the ideal outcome. Ask for it naturally once the client seems ready, don't wait for them to ask.

HOW TO TALK
- This is a voice conversation. Keep responses short, natural, and conversational -- one or two sentences at a time, not a monologue.
- Never repeat a scripted greeting or pitch verbatim if you've already said it earlier in the call.
- If the client interrupts or changes topic, follow them. Don't force the conversation back to where you were.

USING search_service_info
Always call search_service_info -- never answer from general knowledge -- whenever the client asks:
- How you compare to competitors or other agencies
- About guarantees, replacement policies, or SLAs
- About timelines for a specific service type
- Any "how are you different" or "why should we choose you" style question
Never make up a differentiator, guarantee detail, or timeline that didn't come from this tool.

USING create_lead
Call create_lead as soon as you know the client's name and at least one concrete requirement (role type and headcount). Call it again if their requirements materially change later in the call. There is no separate booking tool -- if the client wants to schedule a meeting, include their preferred_meeting_time in this same call. Always ask for the client's email before the call ends if you don't already have it, so we can send them a summary of what was discussed and confirm any meeting request.

When a client gives you their email or phone number, always read it back to them clearly and ask them to confirm it's correct before proceeding -- spoken emails and phone numbers are easy to mishear, so verification matters here specifically. When a client spells their email letter-by-letter, assume all letters are lowercase unless they say otherwise, then confirm.

WHEN TO ESCALATE TO A HUMAN
Use the escalate_to_human tool when:
- The client explicitly asks to speak to a person.
- The request is legal/contractual in nature (custom contract terms, SLAs beyond standard, compliance questions).
- The client is frustrated or the conversation has stalled after you've tried to resolve an objection twice.
- The deal size is unusually large (100+ hires) and likely needs a named account manager.
When you escalate, summarize the conversation so far in the handoff -- the human should not have to ask the client to repeat themselves.
Before escalating to a human, always ask for the client's name and best contact method (email or phone) if you don't already have them -- the specialist needs to know who to follow up with.
Only call escalate_to_human ONCE per conversation. Wait until you have collected and confirmed both the client's name and their contact info (or they explicitly decline to provide one) before escalating -- do not escalate on partial information and then escalate again later in the same call.

Never use tools for information you can just answer conversationally -- only call a tool when you need real data (pricing, booking, lead capture, service info, escalation).

Never invent or provide contact information (email, phone number) for Talentbridge Consulting yourself -- if asked directly, say a specialist will provide that once escalated.

If you tell the client you're doing something -- checking pricing, creating a lead, escalating -- always follow through and confirm the result before moving to a new topic or acting on a new request in the same turn. If the client adds a new request in the same message where they confirm an earlier one (e.g. "go ahead, also I want to talk to a human"), complete the already-promised action first, then handle the new request.

If a tool call fails or you're not sure whether it succeeded, say so plainly and retry once -- never guess at or invent a reason for the failure.

When a tool call returns, only report what its result actually contains. Never invent confirmation details, links, or event IDs. If a calendar booking or email confirmation could not be completed (result shows created: false or sent: false), tell the client plainly that their request has been logged and the team will confirm manually -- do not claim it succeeded or fabricate a link.
```

**Important — separate from the top-level System Message:** each Agora
Custom Tool also has its own **Agent Function → Function description**
field (distinct from the tool's general "Description" field on the config
page). That per-tool description is what the LLM actually reads at
function-call time to decide *when* to invoke it, so it needs to carry the
same behavioral detail as the prompt above, not a placeholder like
`"creating a lead"`. Keep both in sync when either changes.

---

## CRM (Google Sheets)

Every `create_lead` call appends a real row to a live Google Sheet acting
as a lightweight CRM view.

- **Sheet:** `<PASTE_YOUR_GOOGLE_SHEET_LINK_HERE>`
- **Tab:** `Leads`
- **Header row:** `Timestamp | Customer Name | Email | Requirements Summary | Preferred Meeting Time`
- **Sharing:** shared with the Google service account (`GOOGLE_SERVICE_ACCOUNT_JSON`), Editor access

If `GOOGLE_SHEETS_ID` or the service account JSON isn't configured, the
write is skipped silently and `sheet.appended` in the tool response is
`false` — lead capture itself never fails because of this (see the
graceful-degradation tests below).

---

## Automated tests

`test_tools.py` — unit/integration tests for the FastAPI backend, run via
`pytest` against an in-memory `TestClient(app)` (no real server, no
network). **Scope note, stated plainly in the file itself:** these tests
check that *our code* is correct — pricing math, discount tiers, logging,
schema handling, retrieval scoring. They do **not** test conversational
behavior (memory across turns, interruption handling, objection handling),
because that reasoning lives entirely inside Agora's managed `gpt-4o-mini`,
not in this codebase. That's what the manual voice evals below are for.

Covers:

- **`get_staffing_quote`** — correct base rates per service type, volume
  discount tiers at the right thresholds, case-insensitive input, clean
  error (not a crash) on unknown service type, 422 on missing field,
  validation errors still logged
- **`search_service_info`** — relevant doc retrieval, competitor queries
  surface the comparison doc, always returns top 2 matches
- **`book_meeting`** (deprecated, kept for reference) — basic booking,
  custom meeting type, graceful degradation without calendar config or with
  an unparseable time
- **`create_lead`** — basic creation, optional fields default empty, no
  crash without email config, calendar skipped with no meeting time,
  calendar *attempted* with a meeting time, sheet row logic accepts the
  meeting time field
- **`escalate_to_human`** — basic escalation, combined contact info still
  triggers confirmation email, no crash without email config, missing
  summary rejected
- **Logging / dashboard / health** — `/health` lists the 4 active tools and
  flags `book_meeting` as deprecated-but-present, tool calls are logged and
  readable via `/logs/calls`, `/dashboard/stats` shape and counts,
  `/dashboard` page loads
- **Email/ASR validation helpers** — confirmation skipped on an invalid
  email, `looks_like_email`, `normalize_email`, extracting an email from a
  combined "email and phone" string (guarding the ASR mis-transcription
  limitation noted above)

Run:

```bash
pip install -r requirements.txt --break-system-packages
pytest test_tools.py -v
```

---

## Manual voice evals

`MANUAL_VOICE_TEST_SCRIPT.md` — a scripted checklist run against the
**live** agent through Agora Console (or a real call), because
conversational behavior — memory across turns, interruption handling,
objection handling, knowing when to escalate — lives entirely inside
Agora's managed LLM and can't be exercised by `pytest`. Each test records
PASS/FAIL plus what actually happened, doubling as demo footage and eval
evidence. Includes, among others:

- **Test 1 — basic tool call (pricing):** confirms `get_staffing_quote`
  actually fires with correct arguments, cross-checked against
  `/logs/calls`, not just a plausible-sounding spoken number
- **Test 2 — requirement change mid-call (memory):** the single most
  important test in the checklist — confirms an updated headcount
  correctly overrides an earlier one for the rest of the call
- **Test 3 — adding a new requirement without dropping the old one**
- …and further tests covering objection handling, escalation triggers, and
  meeting booking through `create_lead`

Record your screen while running through it — it serves as both demo
footage and eval evidence.

---

## Running locally

```bash
pip install -r requirements.txt --break-system-packages
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Real email (Resend) — internal + client confirmations
1. Sign up free at resend.com, copy your API key
2. Set env vars: `RESEND_API_KEY`, `ESCALATION_EMAIL_TO` (your team inbox)
3. Sandbox mode only sends to the address you signed up with — fine for
   demo purposes

### Real Calendar + Sheets (Google, shared service account)
1. Google Cloud Console → enable **Google Calendar API** and **Google
   Sheets API**
2. IAM & Admin → Service Accounts → create one → download its JSON key
3. Share your Google Calendar with that service account's email, "Make
   changes to events" permission
4. Create a Google Sheet with a tab named `Leads`, header row:
   `Timestamp | Customer Name | Email | Requirements Summary | Preferred Meeting Time`.
   Share it with the same service account, Editor access
5. Set env vars: `GOOGLE_SERVICE_ACCOUNT_JSON` (full JSON as one line),
   `GOOGLE_CALENDAR_ID` (your calendar email), `GOOGLE_SHEETS_ID` (from the
   sheet's URL), optionally `MEETING_TIMEZONE` (defaults to `Asia/Kolkata`)

Then expose the running service publicly so Agora Custom Tools can reach it
— ngrok for quick local testing, Render for anything actually demoed (see
below for why).

---

## Deployment on Render

The backend is deployed as a **Render Web Service** (Python/FastAPI),
publicly reachable so Agora's Custom Tools can call it over plain HTTPS —
no tunnel needed once deployed, which avoids the "tunnel closed mid-demo"
failure mode that ngrok has.

- **Base URL:** `https://close-agent-backend.onrender.com`
- **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
- **Build command:** `pip install -r requirements.txt`

Each Agora Custom Tool's **Request URL** points at this base URL plus its
tool path, e.g.:

```
https://close-agent-backend.onrender.com/tools/create_lead
https://close-agent-backend.onrender.com/tools/get_staffing_quote
https://close-agent-backend.onrender.com/tools/search_service_info
https://close-agent-backend.onrender.com/tools/escalate_to_human
```

**Environment variables to set on the Render service** (Settings →
Environment), same as local:

| Variable | Purpose |
|---|---|
| `RESEND_API_KEY` | Real email sending (lead + escalation confirmations) |
| `ESCALATION_EMAIL_TO` | Internal team inbox for escalations |
| `ESCALATION_EMAIL_FROM` | Optional, defaults to `onboarding@resend.dev` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service account credentials (Calendar + Sheets), as one JSON line |
| `GOOGLE_CALENDAR_ID` | Calendar to book real events into |
| `GOOGLE_SHEETS_ID` | Target spreadsheet for the CRM |
| `GOOGLE_SHEETS_RANGE` | Optional, defaults to `Leads!A:E` |
| `MEETING_TIMEZONE` | Optional, defaults to `Asia/Kolkata` |

> If any of these are missing on Render specifically (as opposed to just
> locally), the corresponding tool degrades gracefully rather than
> crashing — but the agent should also be told (via the prompt rule above)
> to report that degraded state honestly instead of claiming success. Worth
> double-checking these are actually set on the deployed instance before a
> demo, not just assumed from local `.env`.

---

## Dashboard & logs

Read-only, no auth, no build step — useful to have open during a live demo
or eval run:

| Endpoint | What it shows |
|---|---|
| `GET /health` | Active vs. deprecated tools |
| `GET /logs/calls?limit=20` | Recent tool calls, args + results — proof a tool actually fired |
| `GET /logs/errors?limit=20` | Rejected payloads with the exact reason, instead of guessing from a vague spoken "there was an issue" |
| `GET /dashboard/stats` | Aggregated counts (quotes given, leads captured, meetings booked, escalations) + recent activity, always reflects local JSONL logs regardless of whether Calendar/Sheets/email are configured |
| `GET /dashboard` | Small auto-refreshing HTML view of the above |

---

## Known limitations / issues found during testing

Documented honestly rather than glossed over, since some of these directly
shaped the architecture above:

- **Agora tool-call dispatch reliability.** Confirmed via server logs
  showing zero incoming requests during live failures, despite direct curl
  calls to the same endpoint always succeeding. First surfaced with
  `book_meeting` (see [Architecture](#architecture)); the same narrate-but-
  don't-call pattern has also been observed with `create_lead` when a
  client bundles a confirmation and a new request (e.g. an escalation ask)
  into the same turn. Mitigated in the prompt by an explicit
  "finish what you promised before moving on" rule, but not eliminated —
  worth re-checking `/logs/calls` against the live transcript after any
  multi-intent turn.
- **Fabricated success details when a tool result is incomplete.** In
  testing, the agent presented a calendar confirmation link when the
  underlying `create_calendar_event` call had almost certainly returned
  `created: false` (no real `event_link` field exists in that case) —
  decoding the "link" it gave showed garbled, non-functional data rather
  than a real Google Calendar `eid`. Mitigated with an explicit
  "only report what the tool result actually contains, never invent a
  link" rule in the prompt; also worth confirming `GOOGLE_SERVICE_ACCOUNT_JSON`
  / `GOOGLE_CALENDAR_ID` / `RESEND_API_KEY` are genuinely set on Render, not
  just assumed.
- **ASR mis-transcription of spelled-out emails.** Multi-turn confirm-and-
  correct loops (as seen in live testing) are expected and handled via the
  read-back-and-confirm prompt rule plus server-side `normalize_email` /
  `looks_like_email` validation — but a mistranscription that happens to
  look like a plausible email locally can't be caught by validation alone.

