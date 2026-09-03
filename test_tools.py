"""
Automated tool-correctness tests for Close's backend.

IMPORTANT SCOPE NOTE (be honest about this in your submission/demo):
These tests check that OUR code is correct -- pricing math, discount
tiers, logging, schema handling, retrieval scoring. They do NOT test
conversational behavior (memory across turns, interruption handling,
objection handling), because that reasoning now lives entirely inside
Agora's managed gpt-4o-mini, not in this codebase. Testing THAT
requires actually talking to the live agent -- see
MANUAL_VOICE_TEST_SCRIPT.md for the scripted checklist to run through
Agora Console or a real call.

This is the correct and honest split for this architecture: unit-test
what you control, live-test what Agora controls. Don't claim these
automated tests cover conversational intelligence -- they don't and
can't, by design of the pivot to Agora-native tool calling.

Run:
    pip install pytest httpx --break-system-packages
    pytest test_tools.py -v
"""

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


# ---------------------------------------------------------------------
# get_staffing_quote
# ---------------------------------------------------------------------

def test_quote_basic_finance():
    r = client.post("/tools/get_staffing_quote", json={"service_type": "finance", "num_candidates": 5})
    data = r.json()
    assert r.status_code == 200
    assert data["base_rate_per_candidate_usd"] == 1200
    assert data["volume_discount_pct"] == 0
    assert data["total_estimate_usd"] == 6000.0


def test_quote_volume_discount_20_tier():
    r = client.post("/tools/get_staffing_quote", json={"service_type": "finance", "num_candidates": 20})
    data = r.json()
    assert data["volume_discount_pct"] == 10
    assert data["final_rate_per_candidate_usd"] == 1080.0
    assert data["total_estimate_usd"] == 21600.0


def test_quote_volume_discount_50_tier():
    r = client.post("/tools/get_staffing_quote", json={"service_type": "finance", "num_candidates": 50})
    data = r.json()
    assert data["volume_discount_pct"] == 20
    assert data["final_rate_per_candidate_usd"] == 960.0
    assert data["total_estimate_usd"] == 48000.0


def test_quote_it_rate():
    r = client.post("/tools/get_staffing_quote", json={"service_type": "it", "num_candidates": 10})
    data = r.json()
    assert data["base_rate_per_candidate_usd"] == 1800
    assert data["total_estimate_usd"] == 18000.0


def test_quote_executive_rate():
    r = client.post("/tools/get_staffing_quote", json={"service_type": "executive", "num_candidates": 2})
    data = r.json()
    assert data["base_rate_per_candidate_usd"] == 4500


def test_quote_case_insensitive_service_type():
    r = client.post("/tools/get_staffing_quote", json={"service_type": "FINANCE", "num_candidates": 5})
    assert r.json()["service_type"] == "finance"


def test_quote_unknown_service_type_returns_error_not_crash():
    r = client.post("/tools/get_staffing_quote", json={"service_type": "legal", "num_candidates": 5})
    assert r.status_code == 200  # should not 500 -- graceful error in the payload
    assert "error" in r.json()


def test_quote_missing_field_returns_422():
    r = client.post("/tools/get_staffing_quote", json={"service_type": "finance"})
    assert r.status_code == 422  # pydantic validation, not a silent failure


def test_quote_marketing_rate():
    r = client.post("/tools/get_staffing_quote", json={"service_type": "marketing", "num_candidates": 5})
    assert r.json()["base_rate_per_candidate_usd"] == 1000


def test_quote_media_rate():
    r = client.post("/tools/get_staffing_quote", json={"service_type": "media", "num_candidates": 5})
    assert r.json()["base_rate_per_candidate_usd"] == 950


def test_validation_error_gets_logged():
    """This is the fix for the 'book_meeting silently failed' bug -- a
    malformed request should now show up in /logs/errors instead of
    vanishing with no trace."""
    client.post("/tools/get_staffing_quote", json={"service_type": "finance"})  # missing num_candidates
    r = client.get("/logs/errors?limit=1")
    errors = r.json()["errors"]
    assert len(errors) >= 1
    assert errors[-1]["path"] == "/tools/get_staffing_quote"


# ---------------------------------------------------------------------
# search_service_info
# ---------------------------------------------------------------------

def test_search_finds_relevant_doc():
    r = client.post("/tools/search_service_info", json={"query": "finance professionals guarantee"})
    data = r.json()
    titles = [res["title"] for res in data["results"]]
    assert "Finance Staffing Service" in titles


def test_search_competitor_query_surfaces_comparison_doc():
    r = client.post("/tools/search_service_info", json={"query": "how do you compare to other agencies"})
    titles = [res["title"] for res in r.json()["results"]]
    assert "How Talentbridge Consulting Compares" in titles


def test_search_always_returns_top_two():
    r = client.post("/tools/search_service_info", json={"query": "hiring"})
    assert len(r.json()["results"]) == 2


# ---------------------------------------------------------------------
# book_meeting
# ---------------------------------------------------------------------

def test_book_meeting_basic():
    r = client.post("/tools/book_meeting", json={"customer_name": "Ananya Rao", "preferred_time": "Thursday 3pm"})
    data = r.json()
    assert data["status"] == "booked"
    assert data["customer_name"] == "Ananya Rao"
    assert data["meeting_type"] == "recruitment team demo"  # default applied


def test_book_meeting_custom_type():
    r = client.post("/tools/book_meeting", json={
        "customer_name": "Ananya Rao", "preferred_time": "Friday 11am", "meeting_type": "contract review",
    })
    assert r.json()["meeting_type"] == "contract review"


def test_book_meeting_without_calendar_config_does_not_crash():
    """No GOOGLE_SERVICE_ACCOUNT_JSON/GOOGLE_CALENDAR_ID set in test
    environment -- booking must still succeed and log gracefully."""
    r = client.post("/tools/book_meeting", json={"customer_name": "Ananya Rao", "preferred_time": "Thursday 3pm"})
    assert r.status_code == 200
    data = r.json()
    assert data["calendar"]["created"] is False  # expected: not configured in test env


def test_book_meeting_unparseable_time_does_not_crash():
    """Even with calendar configured, a preferred_time that can't be
    parsed into a real date should degrade gracefully, not 500."""
    r = client.post("/tools/book_meeting", json={"customer_name": "Ananya Rao", "preferred_time": "whenever works"})
    assert r.status_code == 200


# ---------------------------------------------------------------------
# create_lead
# ---------------------------------------------------------------------

def test_create_lead_basic():
    r = client.post("/tools/create_lead", json={
        "customer_name": "Ananya Rao", "email": "ananya@example.com", "requirements_summary": "50 finance + IT hires",
    })
    data = r.json()
    assert data["status"] == "lead_logged"
    assert data["requirements_summary"] == "50 finance + IT hires"


def test_create_lead_optional_fields_default_empty():
    r = client.post("/tools/create_lead", json={"customer_name": "Ananya Rao"})
    data = r.json()
    assert data["email"] == ""
    assert data["requirements_summary"] == ""


def test_create_lead_without_email_config_does_not_crash():
    """No RESEND_API_KEY/ESCALATION_EMAIL_TO set in test environment --
    lead capture must still succeed and log gracefully, not error out."""
    r = client.post("/tools/create_lead", json={"customer_name": "Ananya Rao", "email": "a@example.com"})
    assert r.status_code == 200
    data = r.json()
    assert data["notification_email"]["sent"] is False  # expected: not configured in test env


# ---------------------------------------------------------------------
# escalate_to_human
# ---------------------------------------------------------------------

def test_escalate_basic():
    r = client.post("/tools/escalate_to_human", json={
        "reason": "client requested legal review",
        "conversation_summary": "Client wants 50 finance hires, asked about custom contract terms",
        "customer_name": "Ananya Rao",
        "contact_info": "ananya@example.com",
    })
    data = r.json()
    assert data["status"] == "escalated"
    assert "specialist" in data["message"].lower()


def test_escalate_defaults_when_contact_details_missing():
    """If the LLM escalates without capturing name/contact (shouldn't
    happen per the tool description, but must not crash if it does),
    these should default to a clear 'Not provided' rather than blank
    or missing fields in the email/log."""
    r = client.post("/tools/escalate_to_human", json={
        "reason": "client requested legal review",
        "conversation_summary": "Client wants 50 finance hires",
    })
    assert r.status_code == 200
    r2 = client.get("/logs/calls?limit=1")
    last = r2.json()["logs"][-1]
    assert last["args"]["customer_name"] == "Not provided"
    assert last["args"]["contact_info"] == "Not provided"


def test_escalate_without_email_config_does_not_crash():
    """No RESEND_API_KEY/ESCALATION_EMAIL_TO set in test environment --
    escalation must still succeed and log gracefully, not error out."""
    r = client.post("/tools/escalate_to_human", json={
        "reason": "test reason", "conversation_summary": "test summary",
        "customer_name": "Test User", "contact_info": "test@example.com",
    })
    assert r.status_code == 200
    r2 = client.get("/logs/calls?limit=1")
    last = r2.json()["logs"][-1]
    assert last["tool"] == "escalate_to_human"
    assert last["result"]["email"]["sent"] is False  # expected: not configured in test env


def test_escalate_requires_summary():
    r = client.post("/tools/escalate_to_human", json={"reason": "client requested legal review"})
    assert r.status_code == 422  # conversation_summary is required, not silently skipped


# ---------------------------------------------------------------------
# health + logging
# ---------------------------------------------------------------------

def test_health_lists_all_five_tools():
    r = client.get("/health")
    tools = r.json()["tools"]
    assert set(tools) == {
        "get_staffing_quote", "search_service_info", "book_meeting", "create_lead", "escalate_to_human",
    }


def test_calls_get_logged_and_are_readable():
    client.post("/tools/get_staffing_quote", json={"service_type": "finance", "num_candidates": 5})
    r = client.get("/logs/calls?limit=1")
    logs = r.json()["logs"]
    assert len(logs) == 1
    assert logs[-1]["tool"] == "get_staffing_quote"


# ---------------------------------------------------------------------
# dashboard / stats
# ---------------------------------------------------------------------

def test_dashboard_stats_returns_expected_shape():
    r = client.get("/dashboard/stats")
    assert r.status_code == 200
    data = r.json()
    assert set(data["counts"].keys()) == {"quotes_given", "leads_captured", "meetings_booked", "escalations"}


def test_dashboard_stats_counts_increase_after_activity():
    before = client.get("/dashboard/stats").json()["counts"]["leads_captured"]
    client.post("/tools/create_lead", json={"customer_name": "Dashboard Test", "email": "dash@example.com"})
    after = client.get("/dashboard/stats").json()["counts"]["leads_captured"]
    assert after == before + 1


def test_dashboard_page_loads():
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Close" in r.text
    assert "text/html" in r.headers["content-type"]


# ---------------------------------------------------------------------
# client confirmation emails
# ---------------------------------------------------------------------

def test_book_meeting_skips_confirmation_without_valid_email():
    r = client.post("/tools/book_meeting", json={"customer_name": "Ananya Rao", "preferred_time": "Thursday 3pm"})
    data = r.json()
    assert data["client_confirmation"]["sent"] is False


def test_looks_like_email_helper():
    from main import _looks_like_email
    assert _looks_like_email("swati@example.com") is True
    assert _looks_like_email("711-930-926") is False
    assert _looks_like_email("Not provided") is False
    assert _looks_like_email("") is False


def test_normalize_email_strips_and_lowercases():
    from main import normalize_email
    assert normalize_email("  Swati@Example.COM  ") == "swati@example.com"
    assert normalize_email("SWATI@GMAIL.COM") == "swati@gmail.com"


def test_looks_like_email_catches_asr_mistranscription():
    """Simulates the real bug reported: ASR mangling a spelled-out
    email with stray whitespace/casing from spelled-out speech should
    normalize to valid. NOTE: a stray transcribed WORD (e.g. literally
    'capital') that lands inside the local-part is syntactically
    indistinguishable from a real username by regex alone --
    'capitalswati@gmail.com' IS a valid email shape even though it's
    wrong. That failure mode needs the system prompt fix (reading the
    address back for confirmation), not server-side validation -- this
    test documents that boundary rather than claiming to solve it."""
    from main import _looks_like_email, normalize_email
    # stray whitespace + mixed case from spelled-out speech -- should normalize to valid
    assert _looks_like_email(normalize_email(" Swati AGG357 @ Gmail.com ".replace(" ", ""))) is True
    # a genuinely malformed address (no domain) is still correctly rejected
    assert _looks_like_email(normalize_email("swati at gmail")) is False
