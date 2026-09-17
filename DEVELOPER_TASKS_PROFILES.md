# Make the engine work for any offer, not just ours

Hi Oussama — this is the next piece of work. Read the problem first; the tasks make more sense once you see it.

---

## The problem, in one paragraph

The engine is good. Sourcing, filtering, scoring, the review queue, capacity, enrolment, analytics — none of that cares what we sell. But **what we sell is written into the Python**. The words "RuyaTech", "ai_audit", "ai_solo_founder" appear in 12 files. `scorer.py` alone mentions them 33 times. So if we want to run this for a second offer — a different service, a different ideal customer, or a client's business — someone has to edit a dozen files by hand and hope they found every place. That is slow, and it is the kind of change that silently breaks scoring.

## The goal

**One file describes the business. The code describes the machine.**

Swapping to a new offer should be: copy a profile file, edit it, write 15 golden cases, run the golden harness. No Python touched.

## What that file looks like

Something like `profiles/ruyatech.toml`:

```toml
[identity]
company = "RuyaTech"
sender_name = "Oussama Ibrahim"
sender_title = "Founder"
one_liner = "a technical agency that builds, rescues and scales SaaS products"
proof_points = [
  "fixed price announced before coding",
  "10+ delivered projects",
  "100% of the code belongs to the client from day one",
]

[offers.ai_audit]
name = "Product Rescue & Scale-Up"
who = "non-technical founder whose AI-built product is breaking under real users"
detail = "full code audit, stabilisation, refactoring — 4 to 8 weeks"
case_study = "took over an AI-generated SaaS that was collapsing, relaunched in 2 weeks, 600 paying members 6 months later"

[segments.ai_solo_founder]
description = "AI-built product owned by a non-technical founder"
offer = "ai_audit"
is_target = true

[icp]
max_headcount = 20
countries = ["United States", "United Kingdom", "Canada", "Australia", "Ireland", "New Zealand"]
founded_year_min = 2020
reject_company_markers = ["agency", "consulting", "dev shop", "..."]
reject_title_markers = ["freelance", "fractional cto", "..."]

[voice]
# Real lines we have sent that earned replies. The generators imitate these.
examples = [
  "Ove is built for girls going through puberty, so the data belongs to minors...",
]
```

The exact shape is yours to design — you will see what the code actually needs better than this sketch does. Two rules: it must be readable by a non-programmer, and nothing business-specific may stay in a `.py` file.

---

## The tasks

Do them in order. Each one should be its own commit and should leave the app working and the tests green. **After every task run `python -m pytest -q` and `python tools/run_golden.py`.** If the golden agreement drops below where it is now (15 of 15 segments), stop and work out why before moving on.

### Task 1 — The profile loader

Create `profile.py` with a `load_profile()` that reads `profiles/<name>.toml`, where the name comes from config or an env var (`LEAD_PROFILE`, default `ruyatech`). Cache it per process the same way `runconfig.load_config()` does.

Return typed dataclasses, not raw dicts — the rest of the code should get `profile.identity.company`, not `profile["identity"]["company"]`.

**Acceptance:** `from profile import load_profile; load_profile().identity.company == "RuyaTech"`. Tests green.

**Don't:** change any prompt or behaviour yet. This task only adds a loader nobody calls.

---

### Task 2 — Move identity, offers and voice out of the code

The easy 60%. These places carry business text that is pure content, no logic:

- `emailer.py` → `EMAIL_PROMPT_TEMPLATE` (the whole offers block, the proof points, the sender signature)
- `personal_line.py` → `EXAMPLES`
- `campaign_fields.py` → `EXAMPLES`, `SUBJECT_EXAMPLES`, `QUESTION_EXAMPLES`
- `scorer.py` → the OFFERS sentence near the top of `SYSTEM_PROMPT`

Build the strings from the profile at call time.

**Critical:** the assembled prompt must come out **byte-identical** to what it is today for the ruyatech profile. Write a test that asserts that — build the prompt, compare to a stored copy of the current text. That test is what lets you refactor without fear.

**Acceptance:** identity/offers/voice appear in the profile file only. The byte-identical test passes. Golden unchanged at 15 of 15.

---

### Task 3 — Move the ICP rules

`prefilter.py` hardcodes the regexes for rejecting agencies, consultancies, dev shops and non-decision titles, and `config.toml` holds the headcount band. The recipe base filters in `golden/bulk_recipes.json` hold the countries and founded-year range.

Move all of it to `[icp]` in the profile. Build the regexes from the marker lists at load time.

Watch out: `AGENCY_TITLE_MARKERS` deliberately does **not** reject plain "CTO" or "engineer" — a technical founder of their own product is a target. There are comments in the file explaining why. Keep that reasoning with the markers when you move them, because the next person will be tempted to "fix" it.

**Acceptance:** `tests/test_prefilter.py` and `tests/test_stage0_golden.py` pass unchanged. The 12 good / 9 bad Stage-0 fixtures still sort correctly.

---

### Task 4 — Move the sequence and field wiring

`config.toml` already has `[apollo.sequences]` with our sequence ids and the Personal Line custom field id. Those are per-business, so they belong in the profile, not in the operational config.

Rule of thumb for what goes where: **config.toml = how the machine runs** (rate limits, caps, delays, which LLM, concurrency). **Profile = what we sell and to whom.** Sequence ids are the second kind.

**Acceptance:** `python tools/enroll_apollo.py --session <id> --dry-run` works exactly as before.

---

### Task 5 — Move the segment taxonomy

This is the risky one. Take your time.

`constants.py` defines `VALID_SEGMENTS`, `TARGET_SEGMENTS`, `OUT_OF_TARGET_SEGMENTS`. Seven files import it. The segments also appear inside `scorer.py`'s prompt and inside `_validate_verdict`'s derivation rules.

Build the segment list from the profile. Keep `NOT_YET_SCORED_STATUSES` and `CONFIDENCE_THRESHOLD` in `constants.py` — those are machine concepts, not business ones.

The derivation logic in `_validate_verdict` (non-technical founder plus unknown build becomes ai_solo_founder, and so on) is currently written against our specific segment names. Make it read the mapping from the profile instead of naming segments literally. If that turns out to be genuinely hard to generalise, **stop and tell me** rather than forcing it — a good answer here might be "the derivation rules live in the profile too, as data".

**Acceptance:** golden holds at 15 of 15. All 231 tests pass. `grep -rn "ai_solo_founder" --include=*.py .` returns nothing outside tests and the profile loader.

---

### Task 6 — Per-profile golden set

A new profile is worthless if you cannot tell whether it works. Move `golden/cases.jsonl` and `golden/fixtures/` under `profiles/ruyatech/golden/`, and add a `--profile` flag to `tools/run_golden.py` and `tools/run_golden_live.py`.

**Acceptance:** `python tools/run_golden.py --profile ruyatech` gives the same result as today.

---

### Task 7 — Prove it with a second profile

Write `profiles/example.toml` for a deliberately different business. Make it genuinely different — a different service, a different ideal customer, different segments. It does not need real golden cases; three or four hand-written ones are enough.

The point is to find out what you got wrong in tasks 1 to 6. Whatever is still stuck in the Python will show up the moment you try this, and that discovery is the real deliverable of the whole piece of work.

**Acceptance:** you can score a lead under the example profile and get a sensible verdict without editing any `.py` file. Write down anything that fought you.

---

## Rules for the whole job

1. **One task, one commit.** Each commit leaves tests green and the app working.
2. **Never change wording while moving it.** Move first, improve later, in a separate commit. Mixing the two makes it impossible to tell whether a golden drop came from the move or the edit.
3. **Golden is the gate.** 15 of 15 segments before and after. If it drops, the refactor is wrong, not the golden set.
4. **No attribution lines in commits.**
5. **When something resists, say so.** If task 5 turns out to need a different design, that is useful information, not a failure. I would rather hear "the derivation rules need to be data" than see them forced into a shape that fights the code.

## What I am not asking for

- Multi-tenant. One profile per running instance for now. No profile column in the database, no per-profile UI. That is a later decision and it depends on whether we end up running this for clients.
- Any change to scoring quality, prompts, or the pipeline. This is a pure move-the-furniture job. The system should behave identically when it is done.

## How to know you are finished

```bash
python -m pytest -q                        # 231 passed
python tools/run_golden.py --profile ruyatech   # 15/15 segments
grep -rn "RuyaTech" --include=*.py . | grep -v tests/   # nothing
```

And the real test: hand the example profile to someone who has never seen the code, and ask them to change the offer. If they can do it without opening a Python file, the job is done.
