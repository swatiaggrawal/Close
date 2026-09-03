# Close -- Voice Sales Agent for Talentbridge Consulting

A real-time voice AI sales and qualification agent built on Agora's Conversational AI Engine, for a staffing/recruitment consultancy demo scenario. Agora's own managed LLM (`gpt-4o-mini`) handles all conversation reasoning, memory, turn-taking, and interruption handling natively -- this repo only exposes plain REST endpoints that Agora calls directly via **Custom Tools**.

## Architecture decision history (worth reading, not just the current state)

Two real pivots happened during development, both documented here rather than hidden, because the reasoning behind them is part of the engineering story:

1. **Custom LLM adapter (Gemini via LangChain) -> Agora-native tool-calling.** We initially built a FastAPI adapter exposing an OpenAI-compatible endpoint in front of Gemini, to get real tool-calling since Agora's native Gemini config didn't expose function-calling. It worked, but added a second LLM hop (~12s latency per turn) on top of Agora's own reasoning -- too slow for real-time voice. Switched to Agora's native Custom Tools calling this service directly, no adapter, no double LLM hop.

2. **Standalone `book_meeting` tool -> merged into `create_lead`.** We built and tested a dedicated booking tool with real Google Calendar integration. It worked reliably when tested in isolation via direct HTTP calls to this backend, but showed a platform-level dispatch issue through Agora -- the LLM would narrate "let me try that again" but the tool call sometimes never reached this service at all (confirmed via server-side logs showing zero incoming requests during failures, despite direct curl calls to the same endpoint always succeeding). Rather than ship an unreliable "star" feature, we consolidated meeting requests into `create_lead` -- the most reliable tool in the whole system -- as an optional field. A meeting request now travels through the same dependable path as lead capture, and still creates a real Calendar event when a time is provided. `book_meeting`'s code is kept in this repo for reference but is no longer registered as an active Agora Custom Tool.

## What's real vs. simulated (say this plainly in the demo video)

**Real:**
- Voice pipeline, conversation reasoning, memory, turn-taking, interruption handling -- all Agora-native (`gpt-4o-mini`)
- Pricing calculation -- real tiered/volume-discount logic across 5 service categories (finance, IT, executive, marketing, media)
- Service info retrieval -- keyword-scored search over a small in-memory corpus (see "RAG" note below)
- **Google Calendar booking** -- real events created via a Google Calendar API service account, triggered through `create_lead`'s optional `preferred_meeting_time` field
- **Google Sheets CRM** -- real rows appended to a live spreadsheet on every lead capture
- **Real email notifications** -- both internal (to the recruitment team) and client-facing confirmations, sent via Resend, for lead capture and escalation
- Full tool-call and error logging to disk, plus a live `/dashboard` view

**Simulated / not fully solved:**
- The standalone `book_meeting` endpoint code still exists and works correctly in isolation, but is not currently wired to Agora due to the platform reliability issue described above
- Client email/phone capture depends on ASR accuracy for spelled-out strings -- mitigated with a confirm-before-proceeding step in the prompt and server-side normalization/validation, but not fully solvable (a mistranscribed word that happens to look like a valid email locally can't be caught by validation alone)

**On "RAG":** `search_service_info` uses keyword-overlap scoring, not embeddings-based retrieval. It correctly grounds answers in real documents rather than letting the model invent facts, which is the functional goal -- but it's precise to call it "retrieval-based," not full RAG, if asked to elaborate on the technique.

## Active tools (4)

| Tool | What it does | Real integrations |
|---|---|---|
| `get_staffing_quote` | Tiered pricing with volume discounts | Pure computation, always reliable |
| `search_service_info` | Answers service/guarantee/competitor questions | Keyword-scored retrieval over 7 docs |
| `create_lead` | Captures lead info, **and optionally books a meeting** if `preferred_meeting_time` is given | Google Sheets row, Google Calendar event, internal + client emails |
| `escalate_to_human` | Hands off to a human with full context, once per call | Internal email with conversation summary, client confirmation email |

## Setup

```bash
pip install -r requirements.txt --break-system-packages
```

### Required
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Real email (Resend) -- internal + client confirmations
1. Sign up free at resend.com, copy your API key
2. Set env vars: `RESEND_API_KEY`, `ESCALATION_EMAIL_TO` (your team inbox)
3. Sandbox mode only sends to the address you signed up with -- fine for demo purposes

### Real Calendar + Sheets (Google, shared service account)
1. Google Cloud Console -> enable **Google Calendar API** and **Google Sheets API**
2. IAM & Admin -> Service Accounts -> create one -> download its JSON key
3. Share your Google Calendar with that service account's email, "Make changes to events" permission
4. Create a Google Sheet with a tab named `Leads`, header row: `Timestamp | Customer Name | Email | Requirements Summary | Preferred Meeting Time`. Share it with the same service account, Editor access
5. Set env vars: `GOOGLE_SERVICE_ACCOUNT_JSON` (full JSON as one line), `GOOGLE_CALENDAR_ID` (your calendar email), `GOOGLE_SHEETS_ID` (from the sheet's URL), optionally `MEETING_TIMEZONE` (defaults to `Asia/Kolkata`)

All of the above degrade gracefully if unconfigured -- every tool still works, it just skips the real-world side effect and logs why.

## Agora Console setup

Register these Custom Tools (Actions tab), each with matching Body template and Parameters JSON -- see inline docstrings in `main.py` for the exact field names each endpoint expects. Attach all 4 active tools to your agent, set LLM vendor to OpenAI / `gpt-4o-mini` / Agora Managed Key, and paste the system prompt (kept in your submission notes) which governs:
- Tracking changing requirements without falling back to stale numbers
- Forcing `search_service_info` calls for competitor/guarantee questions rather than answering from general knowledge
- Capturing name + email before a call ends
- Reading back and confirming spoken emails/phone numbers before use
- Escalating exactly once per call, only after contact details are confirmed

**Important:** confirm `turn_detection` / Start of Speech / interrupt settings are enabled in the agent's Advanced config -- this is what makes interruption handling actually work, and it's a separate screen from the Custom Tools setup.

## Testing

### Automated (tool correctness -- 38 tests)
```bash
pytest test_tools.py -v
```
Covers pricing math, discount tiers, retrieval scoring, email normalization/extraction (including a real bug found in live testing: escalation's `contact_info` field can legitimately contain both an email and phone together), calendar/sheets graceful degradation, and dashboard stats.

**Scope note:** these test tool *logic*, not conversational *behavior* -- that reasoning lives in Agora's managed LLM, not this code, so it can't be unit tested this way.

### Manual (conversational behavior)
See `MANUAL_VOICE_TEST_SCRIPT.md` for the scripted checklist covering memory, interruption, objection handling, retrieval grounding, and the consolidated lead+meeting flow, run against the live agent through Agora.

## Live dashboard

`GET /dashboard` -- an auto-refreshing ops view (quotes given, leads captured, meetings booked, escalations) reading directly from the same logs every tool writes to. No auth, no build step -- meant for demoing and quick sanity checks, not production use.

## Known limitations, stated plainly

1. **Booking reliability was the reason for the architecture consolidation above** -- not fully solved, worked around by routing through the more reliable `create_lead` path instead.
2. **ASR mistranscription of spelled-out contact details** is a fundamental voice-interface challenge, mitigated (confirm-before-use, server-side normalization) but not eliminated.
3. **Retrieval is keyword-based, not embeddings-based** -- correctly grounds answers, but is a simpler technique than full RAG.

## Files

- `main.py` -- FastAPI backend, 4 active Custom Tools + 1 deprecated-but-present endpoint
- `test_tools.py` -- 38 automated tests
- `MANUAL_VOICE_TEST_SCRIPT.md` -- scripted conversational eval checklist
- `requirements.txt`
- `logs/` -- created automatically: `tool_calls.jsonl`, `crm_leads.jsonl`, `calendar_bookings.jsonl`, `escalations.jsonl`, `request_errors.jsonl`
