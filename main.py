"""
Close -- Agora-Native Custom Tools Backend
--------------------------------------------
ARCHITECTURE (post-pivot):
Agora's own managed LLM (gpt-4o-mini) handles ALL conversation reasoning,
turn-taking, and interruption handling natively. This service does NOT
talk to any LLM -- it only exposes plain REST endpoints that Agora calls
directly via its Custom Tools feature (Actions tab in the agent config).

This replaces an earlier attempt that ran a custom FastAPI adapter in
front of Gemini to expose an OpenAI-compatible /v1/chat/completions
route. That worked, but added a real LLM round trip inside this service
(on top of Agora's own reasoning), which pushed latency to ~12s per
turn -- too slow for real-time voice. Agora's native Custom Tools path
has gpt-4o-mini call these endpoints directly, no adapter, no double
LLM hop, no tunnel-reachability mystery.

DEMO SCENARIO (Talentbridge Consulting):
Close is a voice sales/qualification agent for a staffing & recruitment
consultancy. A client calls in looking to hire finance professionals,
changes headcount mid-call, asks about pricing, adds an IT hiring
requirement, and Close is expected to track all of it and move the call
toward a booked meeting.

WHAT'S REAL VS. SIMULATED (say this plainly in the demo video):
- REAL: voice pipeline + reasoning + turn-taking + interruption handling
  + conversation memory (all Agora-native, gpt-4o-mini), pricing
  calculation (real tiered/volume logic here), service info retrieval
  (keyword-scored mini-RAG over an in-memory corpus, real computation).
- SIMULATED for this round: CRM and calendar are logged locally as
  JSONL files, not wired to a real CRM/calendar product. Human
  escalation logs a structured handoff payload instead of ringing an
  actual person.

Run:
    pip install fastapi uvicorn pydantic --break-system-packages
    uvicorn main:app --host 0.0.0.0 --port 8000

Then expose publicly (ngrok for quick testing, Render for anything you
actually demo on -- see README for why).
"""

import os
import time
import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Optional

import requests
import dateparser
from google.oauth2 import service_account
from googleapiclient.discovery import build as google_build
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("close-tools")

app = FastAPI(title="Close -- Agora Custom Tools Backend")

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
CALL_LOG_PATH = LOG_DIR / "tool_calls.jsonl"
ESCALATION_LOG_PATH = LOG_DIR / "escalations.jsonl"
CRM_LOG_PATH = LOG_DIR / "crm_leads.jsonl"
CALENDAR_LOG_PATH = LOG_DIR / "calendar_bookings.jsonl"
ERROR_LOG_PATH = LOG_DIR / "request_errors.jsonl"

# ---------------------------------------------------------------------
# Real escalation email (Resend). This is the one "simulated" feature
# upgraded to something genuinely real for this round: escalating a
# call now actually sends an email, not just a local log line.
#
# Setup (2 minutes):
#   1. Sign up free at https://resend.com
#   2. Copy your API key, set env var RESEND_API_KEY on Render
#   3. Set env var ESCALATION_EMAIL_TO to the inbox that should receive
#      handoffs (their sandbox sender can email your own verified
#      account address with zero domain setup -- perfect for a demo)
# If these env vars aren't set, escalation still works exactly as
# before (logs locally) -- it just skips the email step silently
# rather than crashing the demo.
# ---------------------------------------------------------------------

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
ESCALATION_EMAIL_TO = os.environ.get("ESCALATION_EMAIL_TO", "")
ESCALATION_EMAIL_FROM = os.environ.get("ESCALATION_EMAIL_FROM", "onboarding@resend.dev")


import re

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
_EMAIL_SEARCH_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def extract_email(text: str) -> str:
    """Fields like escalate_to_human's contact_info are documented to
    hold EITHER an email or phone -- but the model sometimes correctly
    captures BOTH together (e.g. 'swati@gmail.com, 011930927'). A
    whole-string email check fails on that even though a real email is
    right there. This searches WITHIN the string instead of requiring
    the whole field to be just an email. Deliberately does NOT strip
    spaces before searching -- doing so previously caused unrelated
    words either side of a real email to fuse into one false match
    (e.g. 'call me at 555-1234 or swati@gmail.com' became one bad
    local-part). Spaces are meaningful word boundaries here."""
    if not text:
        return ""
    match = _EMAIL_SEARCH_RE.search(text.lower())
    return match.group(0) if match else ""


def normalize_email(value: str) -> str:
    """Strips whitespace and lowercases -- emails are effectively
    case-insensitive in practice, and voice transcription of spelled-out
    addresses (e.g. a client saying 'capital S' while spelling their
    name) can otherwise leave stray casing or whitespace that trips up
    stricter validators like Resend's."""
    if not value:
        return ""
    return value.strip().replace(" ", "").lower()


def _looks_like_email(value: str) -> bool:
    """Real regex validation (not just an '@' check) -- catches
    malformed addresses from ASR mistranscription (e.g. a literal word
    like 'capital' or a stray space ending up inside the string)
    BEFORE we waste a Resend API call on something that will 422."""
    if not value:
        return False
    return bool(_EMAIL_RE.match(normalize_email(value)))


def send_notification_email(subject: str, body_text: str, to_address: str) -> dict:
    """Sends a real email via Resend to a given address. Returns a
    status dict; NEVER raises -- a flaky email API should never take
    down the call. Used for BOTH internal team notifications and
    client-facing confirmations, just with a different to_address."""
    if not RESEND_API_KEY or not to_address:
        return {"sent": False, "detail": "email not configured or no recipient address available"}

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": f"Close (Talentbridge Consulting) <{ESCALATION_EMAIL_FROM}>",
                "to": [to_address],
                "subject": subject,
                "text": body_text,
            },
            timeout=10,
        )
        if resp.status_code >= 400:
            return {"sent": False, "detail": f"Resend returned {resp.status_code}: {resp.text[:200]}"}
        return {"sent": True, "detail": "email dispatched"}
    except Exception as e:
        return {"sent": False, "detail": f"email send failed: {e}"}


def send_escalation_email(reason: str, conversation_summary: str, customer_name: str, contact_info: str) -> dict:
    subject = f"Call escalation: {customer_name} -- {reason[:60]}"
    body = (
        f"A live call was escalated to a human specialist.\n\n"
        f"Client name: {customer_name}\n"
        f"Contact info: {contact_info}\n\n"
        f"Reason: {reason}\n\n"
        f"Conversation summary:\n{conversation_summary}\n"
    )
    return send_notification_email(subject, body, ESCALATION_EMAIL_TO)


def send_lead_email(customer_name: str, email: str, requirements_summary: str) -> dict:
    subject = f"New lead captured: {customer_name}"
    body = (
        f"A new lead was captured by Close.\n\n"
        f"Client name: {customer_name}\n"
        f"Client email: {email or 'Not provided'}\n\n"
        f"Requirements:\n{requirements_summary or 'Not yet specified'}\n"
    )
    return send_notification_email(subject, body, ESCALATION_EMAIL_TO)


def send_client_confirmation(to_address: str, subject: str, body_text: str) -> dict:
    """Client-facing confirmation -- this is the 'follow-up creation'
    outcome from the problem statement, made real rather than just
    logged. Extracts an email from anywhere in to_address (handles
    fields like contact_info that may legitimately contain 'email,
    phone' together) rather than requiring the whole field to be
    purely an email. Skips cleanly if no email can be found -- never
    an error, since a phone-only contact is a valid, expected case."""
    found = extract_email(to_address)
    if not found:
        return {"sent": False, "detail": f"skipped -- no email found in '{to_address}'"}
    return send_notification_email(subject, body_text, found)


# ---------------------------------------------------------------------
# Real calendar booking (Google Calendar API). This is the second
# "simulated -> real" upgrade: book_meeting now creates an actual
# calendar event when configured, not just a local log line.
#
# Setup (~30-45 min, one time):
#   1. In Google Cloud Console: enable the Calendar API for a project.
#   2. Create a Service Account, download its JSON key.
#   3. Share your Google Calendar with that service account's email
#      (grant "Make changes to events" permission).
#   4. Set env vars on Render:
#        GOOGLE_SERVICE_ACCOUNT_JSON = <the full JSON key, as one line>
#        GOOGLE_CALENDAR_ID = <your calendar's email/ID, usually your Gmail>
#        MEETING_TIMEZONE = e.g. "Asia/Kolkata" (defaults to that if unset)
# If these aren't set, or the client's preferred_time can't be parsed
# into a real date/time, this silently falls back to local-log-only --
# it never crashes the call.
#
# IMPORTANT: this makes booking real WHEN the tool successfully fires.
# It does NOT fix the separate, already-diagnosed Agora-side issue
# where book_meeting sometimes never gets called when chained right
# after another tool in the same turn -- that's a platform-level
# dispatch issue upstream of this code, unrelated to whether the
# calendar write itself is real or simulated.
# ---------------------------------------------------------------------

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")
MEETING_TIMEZONE = os.environ.get("MEETING_TIMEZONE", "Asia/Kolkata")
MEETING_DURATION_MINUTES = 30

_calendar_service = None


def _get_calendar_service():
    global _calendar_service
    if _calendar_service is not None:
        return _calendar_service
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_CALENDAR_ID:
        return None
    try:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        _calendar_service = google_build("calendar", "v3", credentials=creds, cache_discovery=False)
        return _calendar_service
    except Exception as e:
        logger.error(f"[calendar] failed to initialize service: {e}")
        return None


def create_calendar_event(customer_name: str, preferred_time: str, meeting_type: str) -> dict:
    """Creates a REAL Google Calendar event. Returns a status dict;
    NEVER raises -- a parsing failure or API hiccup should never take
    down the call, it just falls back to local-log-only for this
    booking."""
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_CALENDAR_ID:
        return {"created": False, "detail": "calendar not configured (missing GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_CALENDAR_ID)"}

    service = _get_calendar_service()
    if service is None:
        return {"created": False, "detail": "calendar service failed to initialize -- check service account JSON"}

    parsed_start = dateparser.parse(
        preferred_time,
        settings={"PREFER_DATES_FROM": "future", "TIMEZONE": MEETING_TIMEZONE, "RETURN_AS_TIMEZONE_AWARE": True},
    )
    if parsed_start is None:
        return {"created": False, "detail": f"could not parse a real date/time from '{preferred_time}'"}

    parsed_end = parsed_start + timedelta(minutes=MEETING_DURATION_MINUTES)

    event_body = {
        "summary": f"{meeting_type} -- {customer_name}",
        "description": f"Booked via Close (voice agent) for {customer_name}.",
        "start": {"dateTime": parsed_start.isoformat(), "timeZone": MEETING_TIMEZONE},
        "end": {"dateTime": parsed_end.isoformat(), "timeZone": MEETING_TIMEZONE},
    }

    try:
        created_event = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event_body).execute()
        return {
            "created": True,
            "detail": "calendar event created",
            "event_link": created_event.get("htmlLink", ""),
            "parsed_start": parsed_start.isoformat(),
        }
    except Exception as e:
        return {"created": False, "detail": f"calendar API call failed: {e}"}


# ---------------------------------------------------------------------
# Real CRM view (Google Sheets). Uses the SAME service account as
# Calendar above -- no new auth setup needed, just one more scope and
# one more thing shared with that service account.
#
# Setup (~10 min, reuses the service account from Calendar setup):
#   1. Create a Google Sheet, add a header row to a tab named "Leads":
#      Timestamp | Customer Name | Email | Requirements Summary | Preferred Meeting Time
#   2. Share that Sheet with the SAME service account email you used
#      for Calendar (Editor access).
#   3. Set env vars on Render:
#        GOOGLE_SHEETS_ID = the spreadsheet ID (the long string in its URL)
#        GOOGLE_SHEETS_RANGE = "Leads!A:E" (default if unset)
# If unconfigured, create_lead still works exactly as before -- it
# just skips the Sheets write silently.
# ---------------------------------------------------------------------

GOOGLE_SHEETS_ID = os.environ.get("GOOGLE_SHEETS_ID", "")
GOOGLE_SHEETS_RANGE = os.environ.get("GOOGLE_SHEETS_RANGE", "Leads!A:E")

_sheets_service = None


def _get_sheets_service():
    global _sheets_service
    if _sheets_service is not None:
        return _sheets_service
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_SHEETS_ID:
        return None
    try:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        _sheets_service = google_build("sheets", "v4", credentials=creds, cache_discovery=False)
        return _sheets_service
    except Exception as e:
        logger.error(f"[sheets] failed to initialize service: {e}")
        return None


def append_lead_to_sheet(customer_name: str, email: str, requirements_summary: str, preferred_meeting_time: str = "") -> dict:
    """Appends a real row to a Google Sheet acting as a lightweight,
    genuinely live CRM view. Returns a status dict; NEVER raises."""
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_SHEETS_ID:
        return {"appended": False, "detail": "sheets not configured (missing GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SHEETS_ID)"}

    service = _get_sheets_service()
    if service is None:
        return {"appended": False, "detail": "sheets service failed to initialize -- check service account JSON"}

    row = [time.strftime("%Y-%m-%d %H:%M:%S"), customer_name, email or "", requirements_summary or "", preferred_meeting_time or ""]
    try:
        service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range=GOOGLE_SHEETS_RANGE,
            valueInputOption="USER_ENTERED",
            body={"values": [row]},
        ).execute()
        return {"appended": True, "detail": "row added to Google Sheet"}
    except Exception as e:
        return {"appended": False, "detail": f"sheets API call failed: {e}"}


@app.exception_handler(RequestValidationError)
async def log_validation_errors(request: Request, exc: RequestValidationError):
    """Without this, a malformed/mismatched request from Agora just 422s
    silently and NEVER reaches log_tool_call() -- meaning failed tool
    calls are invisible in /logs/calls. This makes failures visible so
    'it didn't work' turns into 'here's exactly what field was wrong.'"""
    try:
        raw_body = (await request.body()).decode("utf-8", errors="replace")
    except Exception:
        raw_body = "<could not read body>"
    record = {
        "path": str(request.url.path),
        "errors": exc.errors(),
        "raw_body_received": raw_body,
        "timestamp": time.time(),
    }
    append_jsonl(ERROR_LOG_PATH, record)
    logger.error(f"[validation_error] {request.url.path} -> {exc.errors()} | raw body: {raw_body}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


def append_jsonl(path: Path, record: dict):
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def log_tool_call(tool_name: str, args: dict, result: dict):
    """Every tool call gets logged here regardless of which specific
    endpoint it hit -- this is what the eval/demo pulls from to show
    'here's a real log, not a claim.'"""
    record = {
        "tool": tool_name,
        "args": args,
        "result": result,
        "timestamp": time.time(),
    }
    append_jsonl(CALL_LOG_PATH, record)
    logger.info(f"[tool_call] {tool_name}({args}) -> {result}")


# ---------------------------------------------------------------------
# Tool 1: Pricing -- real tiered + volume-discount calculation
# ---------------------------------------------------------------------

_SERVICE_RATES = {
    "finance": 1200,    # per-candidate placement fee, USD
    "it": 1800,
    "executive": 4500,
    "marketing": 1000,
    "media": 950,
}


class StaffingQuoteRequest(BaseModel):
    service_type: str
    num_candidates: int


@app.post("/tools/get_staffing_quote")
async def get_staffing_quote(req: StaffingQuoteRequest):
    """Agora Custom Tool: get a pricing quote for a staffing service.
    service_type must be one of: finance, it, executive, marketing, media."""
    key = req.service_type.lower().strip()
    rate = _SERVICE_RATES.get(key)

    if rate is None:
        result = {"error": f"Unknown service_type '{req.service_type}'. Valid types: finance, it, executive, marketing, media."}
        log_tool_call("get_staffing_quote", req.model_dump(), result)
        return result

    if req.num_candidates >= 50:
        discount = 0.20
    elif req.num_candidates >= 20:
        discount = 0.10
    else:
        discount = 0.0

    per_candidate = round(rate * (1 - discount), 2)
    total = round(per_candidate * req.num_candidates, 2)

    result = {
        "service_type": key,
        "num_candidates": req.num_candidates,
        "base_rate_per_candidate_usd": rate,
        "volume_discount_pct": int(discount * 100),
        "final_rate_per_candidate_usd": per_candidate,
        "total_estimate_usd": total,
    }
    log_tool_call("get_staffing_quote", req.model_dump(), result)
    return result


# ---------------------------------------------------------------------
# Tool 2: Service info retrieval -- keyword-scored mini-RAG
# ---------------------------------------------------------------------

_SERVICE_DOCS = [
    {
        "id": "finance-staffing",
        "title": "Finance Staffing Service",
        "text": (
            "Talentbridge Consulting sources finance professionals including accountants, "
            "financial analysts, controllers, and CFO-level candidates. Standard "
            "placements are filled within 3 to 4 weeks. All candidates are "
            "pre-screened with a 90-day replacement guarantee."
        ),
    },
    {
        "id": "it-staffing",
        "title": "IT Staffing Service",
        "text": (
            "Talentbridge Consulting places IT roles including software engineers, DevOps, "
            "QA, and IT project managers. Typical time-to-fill is 2 to 3 weeks for "
            "mid-level roles and up to 6 weeks for specialized senior roles. "
            "Includes technical screening before candidates are presented."
        ),
    },
    {
        "id": "executive-search",
        "title": "Executive Search",
        "text": (
            "Executive search covers VP-level and C-suite hires. Engagements are "
            "retained, run 6 to 10 weeks, and include a dedicated search "
            "consultant and a 12-month replacement guarantee."
        ),
    },
    {
        "id": "marketing-staffing",
        "title": "Marketing Staffing Service",
        "text": (
            "Talentbridge Consulting places marketing roles including content "
            "strategists, growth marketers, brand managers, and marketing "
            "coordinators. Typical time-to-fill is 2 to 4 weeks. Candidates are "
            "screened for portfolio quality and campaign experience relevant to "
            "the client's industry."
        ),
    },
    {
        "id": "media-staffing",
        "title": "Media Staffing Service",
        "text": (
            "Talentbridge Consulting sources media professionals including "
            "editors, video producers, and social media managers. Time-to-fill "
            "is typically 2 to 3 weeks. All media placements include the "
            "standard 90-day replacement guarantee."
        ),
    },
    {
        "id": "competitor-comparison",
        "title": "How Talentbridge Consulting Compares",
        "text": (
            "Unlike flat-fee staffing agencies, Talentbridge Consulting offers volume-based "
            "pricing that reduces per-candidate cost as headcount grows, and "
            "every placement includes a replacement guarantee window, which many "
            "competitors charge extra for."
        ),
    },
    {
        "id": "sla-guarantee",
        "title": "Guarantee & SLA Policy",
        "text": (
            "If a placed candidate leaves or is let go within the guarantee "
            "window (90 days for finance/IT, 12 months for executive search), "
            "Talentbridge Consulting provides a replacement at no additional placement fee."
        ),
    },
]


def _score(query: str, text: str) -> int:
    q_words = set(w.lower().strip(".,?!") for w in query.split())
    t_words = set(w.lower().strip(".,?!") for w in text.split())
    return len(q_words & t_words)


class ServiceInfoRequest(BaseModel):
    query: str


@app.post("/tools/search_service_info")
async def search_service_info(req: ServiceInfoRequest):
    """Agora Custom Tool: search Talentbridge Consulting's service info (staffing
    services, guarantees, timelines, competitor comparisons) to answer
    a client question accurately instead of guessing."""
    scored = sorted(_SERVICE_DOCS, key=lambda d: _score(req.query, d["title"] + " " + d["text"]), reverse=True)
    top = scored[:2]
    result = {"query": req.query, "results": [{"title": d["title"], "text": d["text"]} for d in top]}
    log_tool_call("search_service_info", req.model_dump(), result)
    return result


# ---------------------------------------------------------------------
# Tool 3: Book meeting -- logged locally (simulated calendar)
# ---------------------------------------------------------------------

class BookMeetingRequest(BaseModel):
    customer_name: str
    preferred_time: str
    meeting_type: Optional[str] = "recruitment team demo"
    customer_email: Optional[str] = ""


@app.post("/tools/book_meeting")
async def book_meeting(req: BookMeetingRequest):
    """Agora Custom Tool: book a meeting with the Talentbridge Consulting
    recruitment team once the client is ready to move forward. Ask for
    the client's email too if you don't have it, so we can send them a
    confirmation."""
    meeting_type = req.meeting_type or "recruitment team demo"
    calendar_result = create_calendar_event(req.customer_name, req.preferred_time, meeting_type)

    confirmation_result = send_client_confirmation(
        req.customer_email,
        f"Your meeting with Talentbridge Consulting is confirmed",
        (
            f"Hi {req.customer_name},\n\n"
            f"This confirms your {meeting_type} with our recruitment team, "
            f"requested for: {req.preferred_time}.\n\n"
            f"We look forward to speaking with you.\n\n"
            f"-- Talentbridge Consulting"
        ),
    )

    record = {
        "status": "booked",
        "customer_name": req.customer_name,
        "time": req.preferred_time,
        "meeting_type": meeting_type,
        "calendar": calendar_result,
        "client_confirmation": confirmation_result,
        "logged_at": time.time(),
    }
    append_jsonl(CALENDAR_LOG_PATH, record)
    log_tool_call("book_meeting", req.model_dump(), record)
    return record


# ---------------------------------------------------------------------
# Tool 4: Create lead -- real CRM row (Google Sheets) + real emails +
# optional meeting request (replaces the standalone booking tool)
# ---------------------------------------------------------------------

class CreateLeadRequest(BaseModel):
    customer_name: str
    email: Optional[str] = ""
    requirements_summary: Optional[str] = ""
    preferred_meeting_time: Optional[str] = ""


@app.post("/tools/create_lead")
async def create_lead(req: CreateLeadRequest):
    """Agora Custom Tool: create or update a lead/CRM record with the
    client's current requirements, and optionally request a meeting at
    the same time. Call this once real qualification details are
    known, and again if requirements materially change. If the client
    wants to schedule a meeting, capture preferred_meeting_time here
    too rather than needing a separate tool -- always ask for their
    email before the call ends if you don't already have it, so a
    lead record and any meeting request can actually reach them."""
    internal_email_result = send_lead_email(req.customer_name, req.email or "", req.requirements_summary or "")
    sheet_result = append_lead_to_sheet(
        req.customer_name, req.email or "", req.requirements_summary or "", req.preferred_meeting_time or ""
    )

    calendar_result = None
    if req.preferred_meeting_time:
        calendar_result = create_calendar_event(req.customer_name, req.preferred_meeting_time, "recruitment team demo")

    if req.preferred_meeting_time:
        confirmation_body = (
            f"Hi {req.customer_name},\n\n"
            f"Thanks for the conversation today. Here's what we've noted so far:\n\n"
            f"{req.requirements_summary or 'We will follow up to confirm your requirements.'}\n\n"
            f"We've also noted your request to meet at: {req.preferred_meeting_time}. "
            f"Our recruitment team will confirm this shortly.\n\n"
            f"-- Talentbridge Consulting"
        )
    else:
        confirmation_body = (
            f"Hi {req.customer_name},\n\n"
            f"Thanks for the conversation today. Here's what we've noted so far:\n\n"
            f"{req.requirements_summary or 'We will follow up to confirm your requirements.'}\n\n"
            f"Our recruitment team will follow up with you shortly.\n\n"
            f"-- Talentbridge Consulting"
        )

    confirmation_result = send_client_confirmation(
        req.email, "Thanks for reaching out to Talentbridge Consulting", confirmation_body
    )

    record = {
        "status": "lead_logged",
        "customer_name": req.customer_name,
        "email": req.email or "",
        "requirements_summary": req.requirements_summary or "",
        "preferred_meeting_time": req.preferred_meeting_time or "",
        "calendar": calendar_result,
        "notification_email": internal_email_result,
        "sheet": sheet_result,
        "client_confirmation": confirmation_result,
        "logged_at": time.time(),
    }
    append_jsonl(CRM_LOG_PATH, record)
    log_tool_call("create_lead", req.model_dump(), record)
    return record


# ---------------------------------------------------------------------
# Tool 5: Escalate to human -- real email handoff + client confirmation
# ---------------------------------------------------------------------

class EscalateRequest(BaseModel):
    reason: str
    conversation_summary: str
    customer_name: Optional[str] = "Not provided"
    contact_info: Optional[str] = "Not provided"


@app.post("/tools/escalate_to_human")
async def escalate_to_human(req: EscalateRequest):
    """Agora Custom Tool: hand off the call to a human recruitment
    specialist. Use for explicit requests to speak to a person,
    legal/contract questions, stalled objections, or unusually large
    deals. Always include a conversation_summary so the human doesn't
    need the client to repeat themselves, AND always try to capture
    customer_name and contact_info (email or phone) before escalating
    -- ask for these explicitly if the client hasn't already given
    them, so the specialist knows who to follow up with."""
    email_result = send_escalation_email(
        req.reason, req.conversation_summary, req.customer_name, req.contact_info
    )
    confirmation_result = send_client_confirmation(
        req.contact_info,
        "We've received your request -- Talentbridge Consulting",
        (
            f"Hi {req.customer_name},\n\n"
            f"A recruitment specialist has the full context of your conversation "
            f"and will follow up with you shortly regarding: {req.reason}.\n\n"
            f"-- Talentbridge Consulting"
        ),
    )

    record = {
        "status": "escalated",
        "reason": req.reason,
        "conversation_summary": req.conversation_summary,
        "customer_name": req.customer_name,
        "contact_info": req.contact_info,
        "email": email_result,
        "client_confirmation": confirmation_result,
        "logged_at": time.time(),
    }
    append_jsonl(ESCALATION_LOG_PATH, record)
    response = {
        "status": "escalated",
        "message": "A recruitment specialist has the full context and will join or follow up shortly.",
    }
    log_tool_call("escalate_to_human", req.model_dump(), {**response, "email": email_result, "client_confirmation": confirmation_result})
    return response


# ---------------------------------------------------------------------
# Health + log inspection endpoints
# ---------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "active_tools": [
            "get_staffing_quote",
            "search_service_info",
            "create_lead",  # now also handles meeting requests via preferred_meeting_time
            "escalate_to_human",
        ],
        "deprecated_endpoints_still_present": [
            "book_meeting"  # kept in code for reference/manual use, no longer called by Agora -- see README
        ],
    }


@app.get("/logs/calls")
async def get_call_logs(limit: int = 20):
    """Quick way to eyeball recent tool calls without SSH-ing in --
    pull this up during the demo recording as proof it's really firing."""
    if not CALL_LOG_PATH.exists():
        return {"logs": []}
    lines = CALL_LOG_PATH.read_text().strip().splitlines()
    return {"logs": [json.loads(l) for l in lines[-limit:]] if lines else []}


@app.get("/logs/errors")
async def get_error_logs(limit: int = 20):
    """Check this whenever a tool 'silently fails' during a live test --
    it shows the EXACT payload Agora sent and why it was rejected,
    instead of guessing from a spoken 'there was an issue' response."""
    if not ERROR_LOG_PATH.exists():
        return {"errors": []}
    lines = ERROR_LOG_PATH.read_text().strip().splitlines()
    return {"errors": [json.loads(l) for l in lines[-limit:]] if lines else []}


def _read_jsonl(path: Path, limit: int) -> list:
    if not path.exists():
        return []
    lines = path.read_text().strip().splitlines()
    return [json.loads(l) for l in lines[-limit:]] if lines else []


@app.get("/dashboard/stats")
async def dashboard_stats():
    """Aggregated counts + recent activity across all tools. Pure
    read-only, works regardless of whether Calendar/Sheets/email are
    configured -- always reflects the local JSONL logs, which are the
    one source of truth every tool writes to no matter what."""
    quotes = _read_jsonl(CALL_LOG_PATH, 500)
    quote_calls = [q for q in quotes if q.get("tool") == "get_staffing_quote"]
    leads = _read_jsonl(CRM_LOG_PATH, 500)
    bookings = _read_jsonl(CALENDAR_LOG_PATH, 500)
    escalations = _read_jsonl(ESCALATION_LOG_PATH, 500)

    return {
        "counts": {
            "quotes_given": len(quote_calls),
            "leads_captured": len(leads),
            "meetings_booked": len(bookings),
            "escalations": len(escalations),
        },
        "recent_leads": leads[-5:],
        "recent_bookings": bookings[-5:],
        "recent_escalations": escalations[-5:],
    }


@app.get("/dashboard")
async def dashboard_page():
    """A tiny live ops dashboard -- auto-refreshing, no build step, no
    auth. Not a replacement for a real product UI, but gives the
    recruitment team (and your demo video) something to actually look
    at instead of raw JSON."""
    from fastapi.responses import HTMLResponse
    html = """
<!DOCTYPE html>
<html>
<head>
<title>Close -- Talentbridge Consulting Ops Dashboard</title>
<meta charset="utf-8">
<style>
  body { font-family: -apple-system, sans-serif; background: #0b0b0d; color: #eee; margin: 0; padding: 32px; }
  h1 { font-size: 22px; margin-bottom: 4px; }
  .sub { color: #888; margin-bottom: 28px; font-size: 13px; }
  .cards { display: flex; gap: 16px; margin-bottom: 32px; flex-wrap: wrap; }
  .card { background: #17161a; border-radius: 10px; padding: 18px 24px; min-width: 140px; }
  .card .num { font-size: 32px; font-weight: 700; color: #e5b45c; }
  .card .label { font-size: 12px; color: #aaa; margin-top: 4px; }
  .section { margin-bottom: 28px; }
  .section h2 { font-size: 15px; color: #e5b45c; margin-bottom: 10px; }
  .row { background: #121114; border-radius: 8px; padding: 10px 14px; margin-bottom: 6px; font-size: 13px; }
  .row .name { font-weight: 600; }
  .row .meta { color: #999; font-size: 12px; }
  .empty { color: #666; font-size: 13px; font-style: italic; }
</style>
</head>
<body>
  <h1>Close -- Live Ops Dashboard</h1>
  <div class="sub">Talentbridge Consulting &middot; auto-refreshes every 5s</div>
  <div class="cards" id="cards"></div>
  <div class="section"><h2>Recent Leads</h2><div id="leads"></div></div>
  <div class="section"><h2>Recent Bookings</h2><div id="bookings"></div></div>
  <div class="section"><h2>Recent Escalations</h2><div id="escalations"></div></div>

<script>
async function refresh() {
  const r = await fetch('/dashboard/stats');
  const data = await r.json();

  document.getElementById('cards').innerHTML = `
    <div class="card"><div class="num">${data.counts.quotes_given}</div><div class="label">Quotes Given</div></div>
    <div class="card"><div class="num">${data.counts.leads_captured}</div><div class="label">Leads Captured</div></div>
    <div class="card"><div class="num">${data.counts.meetings_booked}</div><div class="label">Meetings Booked</div></div>
    <div class="card"><div class="num">${data.counts.escalations}</div><div class="label">Escalations</div></div>
  `;

  const renderRows = (items, fields) => {
    if (!items.length) return '<div class="empty">Nothing yet.</div>';
    return items.slice().reverse().map(item => {
      const primary = fields.primary.map(f => item[f]).filter(Boolean).join(' -- ');
      const meta = fields.meta.map(f => item[f]).filter(Boolean).join(' | ');
      return `<div class="row"><div class="name">${primary}</div><div class="meta">${meta}</div></div>`;
    }).join('');
  };

  document.getElementById('leads').innerHTML = renderRows(data.recent_leads, {primary: ['customer_name'], meta: ['email', 'requirements_summary']});
  document.getElementById('bookings').innerHTML = renderRows(data.recent_bookings, {primary: ['customer_name'], meta: ['time', 'meeting_type']});
  document.getElementById('escalations').innerHTML = renderRows(data.recent_escalations, {primary: ['customer_name'], meta: ['reason']});
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
    return HTMLResponse(content=html)
