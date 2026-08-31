# Manual Voice Test Script for Close

## Why this exists (read this first)

Your tool logic (`test_tools.py`) is automated and already passing --
that proves pricing math, retrieval, and logging are correct. But
conversational behavior -- memory across turns, interruption handling,
objection handling, knowing when to escalate -- lives entirely inside
Agora's managed `gpt-4o-mini`, not in your code. That can only be
tested by actually talking to the live agent.

Run through this checklist after wiring up the Custom Tools in Agora
Console. Record your screen while you do it -- this doubles as your
demo footage AND your eval evidence.

For each test, note PASS/FAIL and what actually happened. Don't just
mark pass because it mostly worked -- write down the exact phrasing if
something felt off, you'll want it for the "known limitations" slide.

---

## Test 1: Basic tool call (pricing)

**Say:** "Hi, I'm looking to hire 20 finance professionals, what would that cost?"

**Expected:** Agent calls `get_staffing_quote` with `service_type=finance, num_candidates=20`, then speaks back a price reflecting the 10% volume discount (~$1080/candidate, ~$21,600 total).

**Check the log after:** `curl http://<your-url>/logs/calls?limit=1` -- confirm the tool actually fired with the right arguments, not just that the agent said a plausible-sounding number.

- [ ] PASS / FAIL
- Notes:

---

## Test 2: Requirement change mid-call (memory)

**Say (multi-turn):**
1. "Hi, I'm looking to hire 20 finance professionals."
2. "Actually, let's change that to 50 candidates instead."
3. "Just to confirm, how many finance hires did I say I needed?"

**Expected:** On turn 3, the agent says 50, not 20. This is the single most important test in this whole checklist -- it's the core claim of your entire pitch.

- [ ] PASS / FAIL
- Notes:

---

## Test 3: Adding a new requirement without dropping the old one

**Say (continuing from Test 2):** "We also need to hire some IT staff. Can you help with that too?"

**Expected:** Agent acknowledges IT as an *additional* need, doesn't ask you to repeat the finance requirement, and can quote IT pricing separately if asked.

- [ ] PASS / FAIL
- Notes:

---

## Test 4: Interruption handling

**Say:** Start a longer question ("Can you tell me more about your executive search process and how long it usually—") and **actually interrupt yourself out loud** partway through with a new question: "—actually, never mind, what's your IT pricing?"

**Expected:** Agent stops, doesn't finish its planned response, and answers the new question. If it plows through the original answer first, that's a fail on `turn_detection.interrupt_mode`.

**If this fails:** check your agent config has `turn_detection.interrupt_mode` set to `"interrupt"`, not `"append"` or `"ignore"`.

- [ ] PASS / FAIL
- Notes:

---

## Test 5: Pricing objection

**Say:** "Honestly, that sounds expensive compared to other agencies."

**Expected:** Agent engages with the objection (reframes around value/volume pricing, maybe calls `search_service_info` for the competitor-comparison doc) instead of just repeating the price or going silent.

- [ ] PASS / FAIL
- Notes:

---

## Test 6: Competitor comparison (retrieval grounding)

**Say:** "How are you different from other staffing agencies?"

**Expected:** Agent calls `search_service_info`, answer should reflect the actual competitor-comparison doc content (volume pricing + replacement guarantee), not a generic made-up answer.

**Check the log:** confirm `search_service_info` actually fired.

- [ ] PASS / FAIL
- Notes:

---

## Test 7: Escalation trigger

**Say:** "This all sounds fine, but I need to discuss custom contract terms with your legal team. Can I just talk to a real person?"

**Expected:** Agent calls `escalate_to_human` with a reasonable `conversation_summary` that reflects what's actually been discussed so far (candidate count, service type), not a blank or generic summary.

- [ ] PASS / FAIL
- Notes:

---

## Test 8: End-to-end booking (full scenario)

**Say (multi-turn, the actual pitch scenario):**
1. "Hi, I'm calling about hiring 20 finance professionals for our team."
2. "Actually, we're scaling up -- make that 50 candidates."
3. "What would that cost us?"
4. "We'd also like to explore IT hiring alongside this."
5. "This sounds good, can we set up a meeting with your recruitment team? I'm free Thursday at 3pm, my name is Ananya Rao."

**Expected:** By the end, `book_meeting` fires with the correct name and time, and ideally `create_lead` was called somewhere along the way with a requirements summary that reflects 50 (not 20) plus the IT add-on.

**This is your money clip for the demo video** -- if you only record one full take, make it this one.

- [ ] PASS / FAIL
- Notes:

---

## After running all 8

1. Pull the full log: `curl http://<your-url>/logs/calls?limit=50 > eval_evidence.json`
2. Count pass/fail, be honest about anything that didn't work -- a mentor asking "did you actually test this" wants to hear "yes, here's what worked and here's the one thing we're still tuning," not a claim that everything was flawless.
3. If Test 2 (memory) or Test 4 (interruption) fail, fix those before anything else -- they're the two things your problem statement is explicitly judged on.
