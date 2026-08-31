# Close -- Agora-Native Backend (Talentbridge Consulting recruitment demo)

Architecture: Agora's own managed LLM (`gpt-4o-mini`) handles all
conversation reasoning, memory, turn-taking, and interruption handling
natively. This service ONLY exposes plain REST endpoints that Agora
calls directly via **Custom Tools** (Actions tab). No LLM adapter, no
LangChain, no Gemini in this version -- that path was tried, worked,
but added a ~12s latency hit from a second LLM hop and was dropped in
favor of this simpler, faster, Agora-native approach.

## What's real vs. simulated (say this plainly in your demo/video)

**Real:**
- Voice pipeline, conversation reasoning, memory, turn-taking, interruption handling -- all Agora-native (`gpt-4o-mini`)
- Pricing calculation -- real tiered/volume-discount logic in `get_staffing_quote`
- Service info retrieval -- real keyword-scored search over a small in-memory corpus (`search_service_info`)
- Full tool-call logging to disk for every request

**Simulated for this round:**
- CRM (`create_lead`) and calendar (`book_meeting`) write to local JSONL logs, not a real CRM/calendar product
- Human escalation (`escalate_to_human`) logs a structured handoff payload, doesn't ring an actual person

This is an honest scope choice for a hackathon demo, not something to hide.

---

## Step 1: Install and run locally

```bash
pip install -r requirements.txt --break-system-packages
uvicorn main:app --host 0.0.0.0 --port 8000
```

Confirm it's up:
```bash
curl http://localhost:8000/health
```
Expected: `{"status":"ok","tools":["get_staffing_quote","search_service_info","book_meeting","create_lead","escalate_to_human"]}`

## Step 2: Run the automated tool tests

```bash
pytest test_tools.py -v
```
Should show 19 passed. These test pricing math, discount tiers, retrieval scoring, and logging -- NOT conversational behavior (see MANUAL_VOICE_TEST_SCRIPT.md for that).

## Step 3: Expose it publicly

**For quick testing:** `ngrok http 8000` -- but you already hit a free-tier ngrok heartbeat timeout mid-session last time. Don't record your final demo over ngrok if you can avoid it.

**For anything you actually demo/record:** deploy to Render's free tier instead. This is a lightweight REST service (no heavy embeddings, no memory-hungry vector DB), so it won't hit the memory cap issues you saw on Knowledge Copilot.

Quick Render deploy:
1. Push this folder to a GitHub repo.
2. On Render: New → Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Deploy, copy the `https://your-app.onrender.com` URL.

## Step 4: Register the 5 Custom Tools in Agora Console

Go to your agent config → **Actions tab** → **Custom Tools** → **Add Tool**, once for each:

### Tool 1: get_staffing_quote
- **Endpoint:** `https://<your-url>/tools/get_staffing_quote`
- **Method:** POST
- **Description:** "Get a pricing quote for a staffing service. Call this whenever the client asks about cost or pricing."
- **Parameters:**
  ```json
  {
    "type": "object",
    "properties": {
      "service_type": { "type": "string", "description": "One of: finance, it, executive" },
      "num_candidates": { "type": "integer", "description": "Number of candidates the client needs" }
    },
    "required": ["service_type", "num_candidates"]
  }
  ```

### Tool 2: search_service_info
- **Endpoint:** `https://<your-url>/tools/search_service_info`
- **Method:** POST
- **Description:** "Search Talentbridge Consulting's service info, guarantees, timelines, and competitor comparisons. Call this for any factual question you're not certain about instead of guessing."
- **Parameters:**
  ```json
  {
    "type": "object",
    "properties": {
      "query": { "type": "string", "description": "The client's question, in their own words" }
    },
    "required": ["query"]
  }
  ```

### Tool 3: book_meeting
- **Endpoint:** `https://<your-url>/tools/book_meeting`
- **Method:** POST
- **Description:** "Book a meeting with the recruitment team once the client is ready to move forward. Call this when they agree to a next step and give a preferred time."
- **Parameters:**
  ```json
  {
    "type": "object",
    "properties": {
      "customer_name": { "type": "string" },
      "preferred_time": { "type": "string", "description": "The client's stated preferred day/time" },
      "meeting_type": { "type": "string", "description": "Optional, defaults to 'recruitment team demo'" }
    },
    "required": ["customer_name", "preferred_time"]
  }
  ```

### Tool 4: create_lead
- **Endpoint:** `https://<your-url>/tools/create_lead`
- **Method:** POST
- **Description:** "Log the client's contact info and current requirements once qualified. Call this when you know their name and at least one concrete requirement, and again if requirements materially change."
- **Parameters:**
  ```json
  {
    "type": "object",
    "properties": {
      "customer_name": { "type": "string" },
      "email": { "type": "string", "description": "Optional" },
      "requirements_summary": { "type": "string", "description": "Current, up-to-date summary of what the client needs -- always use the LATEST numbers if they changed mid-call" }
    },
    "required": ["customer_name"]
  }
  ```

### Tool 5: escalate_to_human
- **Endpoint:** `https://<your-url>/tools/escalate_to_human`
- **Method:** POST
- **Description:** "Hand off to a human recruitment specialist. Use when the client explicitly asks for a person, raises a legal/contract question, or the conversation has stalled after two attempts to resolve an objection."
- **Parameters:**
  ```json
  {
    "type": "object",
    "properties": {
      "reason": { "type": "string" },
      "conversation_summary": { "type": "string", "description": "Summarize everything discussed so far so the human doesn't need the client to repeat themselves" }
    },
    "required": ["reason", "conversation_summary"]
  }
  ```

After adding all 5, attach them to your agent, save, and **republish the agent config** -- changes don't take effect on a running agent until you do.

## Step 5: Sanity-check the config

- LLM vendor should be Agora-managed `gpt-4o-mini` (not a custom LLM URL -- you're not using the adapter path anymore).
- `turn_detection.interrupt_mode` should be `"interrupt"` -- this is what makes interruption handling actually work. Check this explicitly, it's easy to leave on a default that doesn't interrupt.
- `llm.max_history` controls how many turns of memory are kept (default 32) -- fine as-is for a demo-length call.

## Step 6: Test it for real

Work through **MANUAL_VOICE_TEST_SCRIPT.md** -- 8 scripted tests covering pricing, memory, interruption, objections, retrieval, escalation, and a full end-to-end booking flow. Record your screen while doing this; it's both your test evidence and usable demo footage.

While testing, keep this open in another tab to confirm tools are actually firing, not just sounding plausible:
```bash
curl https://<your-url>/logs/calls?limit=10
```

## Files in this folder

- `main.py` -- the 5 Custom Tools, plain FastAPI, no LLM calls
- `test_tools.py` -- 19 automated tests for tool correctness (run with `pytest test_tools.py -v`)
- `MANUAL_VOICE_TEST_SCRIPT.md` -- scripted checklist for testing conversational behavior live through Agora
- `requirements.txt`
- `logs/` -- created automatically at runtime: `tool_calls.jsonl`, `crm_leads.jsonl`, `calendar_bookings.jsonl`, `escalations.jsonl`

## What changed from the earlier Gemini/LangChain version

That version is fully dropped from the active architecture. It's still worth mentioning in your submission as "explored, didn't use" -- it shows you evaluated the tradeoff (adapter flexibility vs. latency) and made a deliberate call, which is a stronger story than never having tried the alternative.
