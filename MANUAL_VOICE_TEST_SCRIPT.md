# Manual Voice Test Script for Close


## Test 1: Basic tool call (pricing)

**Say:** "Hi, I'm looking to hire 20 finance professionals, what would that cost?"

**Expected:** Agent calls `get_staffing_quote` with `service_type=finance, num_candidates=20`, then speaks back a price reflecting the 10% volume discount (~$1080/candidate, ~$21,600 total).

**Check the log after:** `curl http://<your-url>/logs/calls?limit=1` -- confirm the tool actually fired with the right arguments, not just that the agent said a plausible-sounding number.

---

## Test 2: Requirement change mid-call (memory)

**Say (multi-turn):**
1. "Hi, I'm looking to hire 20 finance professionals."
2. "Actually, let's change that to 50 candidates instead."
3. "Just to confirm, how many finance hires did I say I needed?"

**Expected:** On turn 3, the agent says 50, not 20. This is the single most important test in this whole checklist -- it's the core claim of your entire pitch.


---

## Test 3: Adding a new requirement without dropping the old one

**Say (continuing from Test 2):** "We also need to hire some IT staff. Can you help with that too?"

**Expected:** Agent acknowledges IT as an *additional* need, doesn't ask you to repeat the finance requirement, and can quote IT pricing separately if asked.


---

## Test 4: Interruption handling

**Say:** Start a longer question ("Can you tell me more about your executive search process and how long it usually—") and **actually interrupt yourself out loud** partway through with a new question: "—actually, never mind, what's your IT pricing?"

**Expected:** Agent stops, doesn't finish its planned response, and answers the new question. If it plows through the original answer first, that's a fail on `turn_detection.interrupt_mode`.

**If this fails:** check your agent config has `turn_detection.interrupt_mode` set to `"interrupt"`, not `"append"` or `"ignore"`.

---

## Test 5: Pricing objection

**Say:** "Honestly, that sounds expensive compared to other agencies."

**Expected:** Agent engages with the objection (reframes around value/volume pricing, maybe calls `search_service_info` for the competitor-comparison doc) instead of just repeating the price or going silent.

---

## Test 6: Competitor comparison (retrieval grounding)

**Say:** "How are you different from other staffing agencies?"

**Expected:** Agent calls `search_service_info`, answer should reflect the actual competitor-comparison doc content (volume pricing + replacement guarantee), not a generic made-up answer.

**Check the log:** confirm `search_service_info` actually fired.

---

## Test 7: Escalation trigger

**Say:** "This all sounds fine, but I need to discuss custom contract terms with your legal team. Can I just talk to a real person?"

**Expected:** Agent calls `escalate_to_human` with a reasonable `conversation_summary` that reflects what's actually been discussed so far (candidate count, service type), not a blank or generic summary.

---

## Test 8: End-to-end booking (full scenario)

**Say (multi-turn, the actual pitch scenario):**
1. "Hi, I'm calling about hiring 20 finance professionals for our team."
2. "Actually, we're scaling up -- make that 50 candidates."
3. "What would that cost us?"
4. "We'd also like to explore IT hiring alongside this."
5. "This sounds good, can we set up a meeting with your recruitment team? I'm free Thursday at 3pm, my name is Swati."

**Expected:** By the end, `book_meeting` fires with the correct name and time, and ideally `create_lead` was called somewhere along the way with a requirements summary that reflects 50 (not 20) plus the IT add-on.

