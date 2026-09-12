# Monday sequence — draft for your approval

Read the finding first. It changes what we should send.

---

## What your own Apollo history says

I pulled every message ever sent from the account and matched replies to them.

| Campaign | People | Sends | Opened | Replied | Reply rate |
|---|---|---|---|---|---|
| March–May, one-off personalised emails (no sequence) | 64 | 130 | 36 | **5** | **7.8%** |
| Ruya — UK Dental Demos (Dentist-UK-1200) | 38 | 95 | 5 | 1 | 2.6% |
| AIAUDIT B1 — Angle C (general audit) | 32 | 93 | 19 | **0** | 0% |
| AIAUDIT v3 — give it away | 18 | 51 | 5 | **0** | 0% |
| AIAUDIT B1 — Angle A (sensitive data) | 11 | 31 | 2 | **0** | 0% |
| AIAUDIT B1 — Angle B (AI-built) | 5 | 15 | 3 | **0** | 0% |

**The AIAUDIT sequences are 0 replies from 66 people.** Opens were fine — 59% on Angle C — so people read them and did not answer. The March emails got the same open rate and five replies.

Honest caveat: 66 people is small. If the true rate were 5%, getting zero is unlikely but not impossible, about a 3% chance. So treat this as a strong lean, not proof. What makes me confident enough to act on it is that the March approach worked at the same sample size.

---

## What was different about the emails that got replies

Every March email was built on something the founder had **recently posted or done**, and every follow-up brought a **new** observation rather than bumping the old one.

Real examples from your sent mail:

> **Subject: between nothing and something**
> Sarah, the LA Business Journal piece gave Solace the framing it's been waiting for. "Grieftech" as a category barely existed a year ago, and Solace is the thing defining it.

> **Subject: Full circle from winner to judge**
> Solène, saw your post about being invited back to Lets Fund Her x Tide as a judge after winning in October. Full-circle moments like that usually arrive later in a founder's journey. Yours showed up…

> **Follow-up, a week later**
> Karima, your headline reads "I build tech you can feel." That's a product principle, not a marketing line. The architecture under it is harder to build than the slogan implies.

And the two replies that came back were answers to short, specific questions:

> Julien — is the multimodal video analysis processing live across all three platforms or still scaling up?

Against that, the AIAUDIT sequence personalises one line, then runs the same body and the same subject for everyone, and closes with no ask: "If things ever get to the point where a bad day would be expensive, we're around."

**Conclusion: the lever is not a better opening line inside generic copy. It is that every touch is visibly about them, and at least one touch asks something easy to answer.**

That is exactly what the system's LinkedIn founder lane produces: the founder's own recent posts. It is harvesting them for the Monday batch right now.

---

## Proposed sequence — "AIAUDIT v4 — their words"

Three touches, days 0 / 3 / 7. Three merge fields, filled per contact by the system. Everything in plain text is fixed copy you approve once.

### Touch 1 — day 0

**Subject:** `{{subject_line}}`
*(system-generated, 3–6 words, drawn from their own situation: "between nothing and something", "Full circle from winner to judge")*

```
Hi {{first_name}},

{{personal_line}}

We audit products that got built fast — the kind where the product works but
nobody outside the team has read the code. Usually a week, fixed price, and
you get the findings whether or not you do anything with us.

{{opening_question}}

Oussama Ibrahim
Founder, RuyaTech
```

`{{personal_line}}` is the observation plus the implication — already generated and in your sheet.
`{{opening_question}}` is one short, specific, easy-to-answer question about their build.

### Touch 2 — day 3

**Subject:** `Re: {{subject_line}}`

```
{{first_name}},

{{second_observation}}

That's the kind of thing that's cheap to check now and expensive to discover
later. Happy to point at the three things I'd look at first, no strings.

Oussama Ibrahim
Founder, RuyaTech
```

`{{second_observation}}` is a **different** detail from a different source than touch 1 — a second post, the careers page, the pricing page.

### Touch 3 — day 7

**Subject:** `Re: {{subject_line}}`

```
{{first_name}}, last one from me.

The five checks are yours to run either way: ruyatech.io/audit

If it ever gets to the point where a bad day would be expensive, we're around.

Oussama Ibrahim
Founder, RuyaTech
```

Touch 3 is kept from your existing AIAUDIT copy — it is good, and it costs nothing.

Every email ends with the opt-out line Apollo appends.

---

## What you need to do in Apollo (about 15 minutes)

1. **Create three contact custom fields** (Settings → Fields → Contact). The API key cannot create them, so this is manual. Name them exactly:
   - `Personal Line` — already exists, do nothing
   - `Subject Line`
   - `Opening Question`
   - `Second Observation`
2. **Create the sequence** "AIAUDIT v4 — their words" with the three steps above, at 0 / 3 / 7 days, sending from the five mailboxes.
3. **Send me the sequence id** (it is in the URL when you open it) and the field ids, or just tell me it is done and I will read them from the API.

If you would rather not create the extra fields, say so and we ship touch 1 personalised and touches 2 and 3 generic. Weaker, but still a real send.

---

## What I do before Monday

- Finish the LinkedIn harvest on the 45-lead batch (running, a few hours).
- Rescore those leads with their posts included, so the hooks come from their own words.
- Generate the four merge values per contact: subject line, personal line, opening question, second observation.
- Put them in a sheet for you to KEEP/CUT, same as the personal lines.
- Dry-run the enrolment so Monday is one command.

---

## Monday batch

45 founders. All personalised, no control arm — at this size a split would only produce noise, and the honest comparison is against your own history: 0 replies from 66 on AIAUDIT, 5 from 64 on the March style.

Success on Monday means at least 2 replies from 45. Below that and the approach needs rethinking before we spend the other 270 leads.
