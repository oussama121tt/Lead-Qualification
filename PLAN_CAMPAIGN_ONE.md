# Campaign One — execution plan

Date: 2026-09-11. Goal: get the first evidence-grounded campaign out through this system and back with real replies, with an A/B that tells us whether the personalised opener beats the plain sequence.

Every step names who does it, what it costs, how long it takes, and what "done" looks like. Steps run in order; nothing in a later step starts before the earlier one is done.

---

## Where we start

| Item | Now |
|---|---|
| Lead bank | 506 sourced, 21 on do-not-contact, 485 usable |
| Scored on Sonnet 5 with the two-axis prompt | 330 |
| Still to score | 152 (134 need a scrape, 15 got fake verdicts during the credit outage, 3 stragglers) |
| Target-segment candidates so far | 275 (48 strong, 160 founder-proven / build unknown, 67 other target) |
| Founder LinkedIn posts harvested | almost none (lane was off for the bulk run) |
| Anthropic credit | empty |
| ScrapeGraphAI credits | 5,894 across 14 keys |
| Apollo credits | 3,418 of 4,000 |

---

## Phase 0 — Unblock (you, 10 minutes)

1. Add Anthropic credit. $30 covers everything in this plan with margin.
2. Push the code: `git push origin merge-lead-tool && git push origin merge-lead-tool:main` (15 commits waiting).
3. Confirm the two yeses: founder lane at 100 profiles/day with 6 posts each, and spending most of the ScrapeGraphAI ring on the evidence pass.

Done when: you tell me "topped up".

---

## Phase 1 — Finish pass one (me, about 45 minutes, about $5)

Score the 152 remaining leads with the current prompt and `high_only` escalation off, so this pass is cheap and only settles segments.

```
LINKEDIN_FOUNDER_LANE=0 python <driver> --mode rescore   # the 15 fake verdicts + stragglers
LINKEDIN_FOUNDER_LANE=0 python <driver> --mode new       # the 134 scrapes + scores
```

Done when: every usable lead has a Sonnet verdict. Expected: about 320 target-segment candidates, 165 rejects or unclear.

---

## Phase 2 — Full-evidence pass on the target segments (me, 3 days background, about 3,500 SGAI credits, about $10)

Config changes, committed before the run:

```toml
[escalation]  mode = "targets"          # every target-segment lead gets web + founder evidence
[linkedin]    founder_lane_enabled = true
              daily_cap = 100            # was 50
              max_posts = 6              # was 12
```

What happens per lead: seven web searches (LinkedIn, Product Hunt, GitHub, X, interviews), the company LinkedIn page, the founder's profile and up to 6 of their own posts with authorship checked in code, then a second Sonnet verdict with all of it. Rejects and unclear leads never spend a credit.

Runs as one background job over about three days because the lane paces itself against LinkedIn blocks. The Ops page shows progress, verdict rate, and the ScrapeGraphAI balance per key. I stop it if the ring drops below 500 credits.

Done when: every target-segment lead has a second verdict. Expected effects: the "build unknown" bucket shrinks sharply, strong-fit count rises, hooks come from what founders actually wrote.

Then: regenerate `worth_emailing.xlsx` and send it to you.

---

## Phase 3 — Prompt polish for hooks (me, 2 hours, reruns nothing)

One prompt change: hooks must be observations ("Saw you store patient records in the app"), never advice ("Highlight how an audit can harden..."). Validated on the 15 golden cases, then applied only to the email-drafting step, so no third scoring pass is needed. Your feedback on the sheet feeds into this.

Done when: golden cases hold at 15/15 segments and hooks read as openers.

---

## Phase 4 — Your review (you, about 1 hour)

1. Sign up on the local app, open the campaign and press the keyboard queue.
2. A on strong fits without reading. Ten seconds on the founder-proven ones. Read the reason line on the rest.
3. Target: about 260 approved. E lets you type your own opener on any card.

Done when: approved count on the Ops page is about 260.

---

## Phase 5 — Sequence setup in Apollo (me with you, 1 hour)

1. Verify or create a custom contact field named `first_line` on the Apollo account. Arm 2 depends on it.
2. Duplicate the three AIAUDIT sequences (Angle A sensitive data, Angle B AI-built, Angle C general audit) into a "personalised" variant whose first email starts with `{{first_line}}`. Follow-ups unchanged.
3. Put the six sequence ids into config.toml under `[apollo.sequences]`, control and personalised.
4. Email prompt: your rewrite, or the current one. Either way the draft's opener becomes the `first_line` value.

Done when: dry-run of the enrol tool lists every approved lead under the correct sequence and arm with a non-empty first line.

---

## Phase 6 — Enrol and send (me, one command, then Apollo sends over 7 days)

1. Split approved leads evenly into arm 1 (control) and arm 2 (personalised) within each offer.
2. `python tools/enroll_apollo.py --campaign N --dry-run`, you read the plan.
3. Flip `[apollo.sequences].enabled = true`, run it for real. Every enrolled lead is written to do-not-contact and export history the same second.
4. Apollo sends from the five mailboxes at about 30 a day each. Arm sizes of about 130 mean the last first-touch goes out in two days and the third touch about a week later.

Done when: the Ops page shows about 260 enrolled and the nightly sync starts matching messages.

---

## Phase 7 — Read the result (both, day 10 to 14)

Nightly sync fills opens, clicks, replies per lead. The analytics page shows reply rate per arm, per offer, per founder profile, per sensitive-data category, with lift against baseline once an arm passes 10 sends.

Decision at day 14:
- Arm 2 reply rate clearly above arm 1: personalised opener wins, build the bespoke chain next.
- No difference: the opener is not the lever, spend the effort on targeting and the offer.
- Arm 1 wins: the hooks are hurting, rewrite before the next campaign.

Reply rate to beat: the manual sends got 5 replies from 93, about 5 percent.

---

## What stays off until Phase 7 is read

Triggers, capacity tuning, the taxonomy widening for the pipeline offer, and the triggers.py refactor. All built, all waiting for data.

---

## Costs and time, whole plan

| Phase | Money | Wall time |
|---|---|---|
| 1. Finish pass one | about $5 Anthropic | 45 min |
| 2. Full evidence | about $10 Anthropic, about 3,500 SGAI credits | 3 days background |
| 3. Hook polish | under $1 | 2 hours |
| 4. Your review | 0 | 1 hour |
| 5. Sequence setup | 0 | 1 hour |
| 6. Send | 0 Apollo credits (contacts already enriched) | 7 days sending |
| 7. Read | 0 | day 10 to 14 |

Roughly $20 and two weeks from "topped up" to a decision backed by real replies.
