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

DEMO SCENARIO (NR Consulting):
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

import time
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
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
    "finance": 1200,   # per-candidate placement fee, USD
    "it": 1800,
    "executive": 4500,
}


class StaffingQuoteRequest(BaseModel):
    service_type: str
    num_candidates: int


@app.post("/tools/get_staffing_quote")
async def get_staffing_quote(req: StaffingQuoteRequest):
    """Agora Custom Tool: get a pricing quote for a staffing service.
    service_type must be one of: finance, it, executive."""
    key = req.service_type.lower().strip()
    rate = _SERVICE_RATES.get(key)

    if rate is None:
        result = {"error": f"Unknown service_type '{req.service_type}'. Valid types: finance, it, executive."}
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
            "NR Consulting sources finance professionals including accountants, "
            "financial analysts, controllers, and CFO-level candidates. Standard "
            "placements are filled within 3 to 4 weeks. All candidates are "
            "pre-screened with a 90-day replacement guarantee."
        ),
    },
    {
        "id": "it-staffing",
        "title": "IT Staffing Service",
        "text": (
            "NR Consulting places IT roles including software engineers, DevOps, "
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
        "id": "competitor-comparison",
        "title": "How NR Consulting Compares",
        "text": (
            "Unlike flat-fee staffing agencies, NR Consulting offers volume-based "
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
            "NR Consulting provides a replacement at no additional placement fee."
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
    """Agora Custom Tool: search NR Consulting's service info (staffing
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


@app.post("/tools/book_meeting")
async def book_meeting(req: BookMeetingRequest):
    """Agora Custom Tool: book a meeting with the NR Consulting
    recruitment team once the client is ready to move forward."""
    record = {
        "status": "booked",
        "customer_name": req.customer_name,
        "time": req.preferred_time,
        "meeting_type": req.meeting_type or "recruitment team demo",
        "logged_at": time.time(),
    }
    append_jsonl(CALENDAR_LOG_PATH, record)
    log_tool_call("book_meeting", req.model_dump(), record)
    return record


# ---------------------------------------------------------------------
# Tool 4: Create lead -- logged locally (simulated CRM)
# ---------------------------------------------------------------------

class CreateLeadRequest(BaseModel):
    customer_name: str
    email: Optional[str] = ""
    requirements_summary: Optional[str] = ""


@app.post("/tools/create_lead")
async def create_lead(req: CreateLeadRequest):
    """Agora Custom Tool: create or update a lead/CRM record with the
    client's current requirements. Call this once real qualification
    details are known, and again if requirements materially change."""
    record = {
        "status": "lead_logged",
        "customer_name": req.customer_name,
        "email": req.email or "",
        "requirements_summary": req.requirements_summary or "",
        "logged_at": time.time(),
    }
    append_jsonl(CRM_LOG_PATH, record)
    log_tool_call("create_lead", req.model_dump(), record)
    return record


# ---------------------------------------------------------------------
# Tool 5: Escalate to human -- logged locally (simulated handoff)
# ---------------------------------------------------------------------

class EscalateRequest(BaseModel):
    reason: str
    conversation_summary: str


@app.post("/tools/escalate_to_human")
async def escalate_to_human(req: EscalateRequest):
    """Agora Custom Tool: hand off the call to a human recruitment
    specialist. Use for explicit requests to speak to a person,
    legal/contract questions, stalled objections, or unusually large
    deals. Always include a conversation_summary so the human doesn't
    need the client to repeat themselves."""
    record = {
        "status": "escalated",
        "reason": req.reason,
        "conversation_summary": req.conversation_summary,
        "logged_at": time.time(),
    }
    append_jsonl(ESCALATION_LOG_PATH, record)
    response = {
        "status": "escalated",
        "message": "A recruitment specialist has the full context and will join or follow up shortly.",
    }
    log_tool_call("escalate_to_human", req.model_dump(), response)
    return response


# ---------------------------------------------------------------------
# Health + log inspection endpoints
# ---------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "tools": [
            "get_staffing_quote",
            "search_service_info",
            "book_meeting",
            "create_lead",
            "escalate_to_human",
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
