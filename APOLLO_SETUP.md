# Apollo setup — step by step

About 15 minutes. Two jobs: create three custom fields, then build one sequence.

I cannot do either through the API (the key is not scoped for field creation, and sequence detail is read-blocked), so this part is yours. Everything after it is mine.

---

## Job 1 — Three custom fields (5 minutes)

You already have one called **Personal Line**. We need three more alongside it.

1. Click your avatar, top right → **Settings**.
2. In the left menu find **Fields** (it may sit under a *Data* or *Objects* heading depending on your plan). If you cannot see it, type "fields" in the settings search box.
3. Make sure you are on the **Contacts** object, not Accounts. The existing *Personal Line* field is on Contacts, so it will be in that list — use it to confirm you are in the right place.
4. Click **New field** / **Add field** and create each of these three. Match the type exactly:

| Field name (type it exactly) | Type |
|---|---|
| `Subject Line` | Text (short text) |
| `Opening Question` | Long text / Text area |
| `Second Observation` | Long text / Text area |

*Personal Line is already a Text area, so Opening Question and Second Observation should match it. Subject Line is short, so plain Text is fine — if your Apollo only offers Text area, that works too.*

5. Save each one. Names must match exactly, including capitals and the space, because I look them up by name.

**When all three exist, tell me "fields done" and I will read their ids from the API and confirm.**

---

## Job 2 — The sequence (10 minutes)

### Create it

1. Top navigation → **Engage** → **Sequences** (or go straight to `app.apollo.io/#/sequences`).
2. Click **New sequence** → **Create from scratch**.
3. Name it exactly: `AIAUDIT v4 — their words`
4. When it asks about sending: choose **Automatic** email, and select all five mailboxes — `wael@ruyaa.io`, `oussama@ruya.care`, `wael@ruya.care`, `oussama@ruyatech.io`, `oussama@ruyaa.io`. Apollo will rotate between them.

### Step 1 — Automatic email, day 0

Click **Add a step** → **Automatic email**. Set the delay to **0 days** (send immediately on enrolment).

**Important: do not type the `{{...}}` tags by hand.** Use the **personalisation / variable picker** in the editor toolbar (usually a `{ }` icon or an "Insert variable" button) and choose the field by name. Apollo's internal tag format for custom fields is not something you should guess, and the picker inserts the right one.

**Subject:** insert the variable **Subject Line**, nothing else.

> If Apollo will not let you put a custom field in the subject, use this fixed subject instead and tell me: `a second pair of eyes on {{company}}`

**Body** — paste this, then replace each bracketed instruction with the variable from the picker:

```
Hi [FIRST NAME variable],

[PERSONAL LINE variable]

We audit products that got built fast — the kind where the product works but
nobody outside the team has read the code. Usually a week, fixed price, and you
get the findings whether or not you do anything with us.

[OPENING QUESTION variable]

Oussama Ibrahim
Founder, RuyaTech
```

Keep the blank lines. They matter — the emails that got replies were airy, not dense.

### Step 2 — Automatic email, day 3

Add a step → **Automatic email** → delay **3 days**.

Tick the option to **reply in the same thread** (Apollo usually shows this as "Reply to previous email" or similar). That gives you the `Re:` subject automatically. If there is no such option, set the subject to `Re: ` followed by the Subject Line variable.

**Body:**

```
[FIRST NAME variable],

[SECOND OBSERVATION variable]

That's the kind of thing that's cheap to check now and expensive to discover
later. Happy to point at the three things I'd look at first, no strings.

Oussama Ibrahim
Founder, RuyaTech
```

### Step 3 — Automatic email, day 7

Add a step → **Automatic email** → delay **4 days** (that lands on day 7 overall — check whether your Apollo counts delays from the previous step or from enrolment, and set it so the gap from step 2 is four days).

Same thread reply again.

**Body:**

```
[FIRST NAME variable], last one from me.

The five checks are yours to run either way: ruyatech.io/audit

If it ever gets to the point where a bad day would be expensive, we're around.

Oussama Ibrahim
Founder, RuyaTech
```

### Before you leave the sequence

- Check the sending schedule is **weekdays only**, business hours in the recipient's timezone if that option exists.
- Leave the sequence **inactive / paused** for now. I enrol people into it; you switch it on when you are happy.
- Confirm the unsubscribe or opt-out footer is enabled, since it is a legal requirement and the old sequences had it.

---

## Job 3 — One test before Monday (2 minutes)

This is the step that catches a broken merge tag before 45 real people see it.

1. In the sequence editor, use **Preview** and pick any contact that already has a Personal Line filled — the probe contact `leadengine.probe@ruyatech.io` has one, I set it earlier as a test.
2. Check the preview shows the actual sentence, not a literal `{{...}}` tag and not a blank.
3. If it shows blank or the raw tag, the variable is wrong. Tell me what you see and I will work out which field it is reading.

---

## Then tell me

Just say **"apollo done"**. I will:

1. Read the three new field ids and the sequence id from the API.
2. Put them into `config.toml`.
3. Generate the four values per contact for all 45 leads.
4. Send you a KEEP/CUT sheet.
5. Dry-run the enrolment so you can see exactly who gets what.

Monday is then one command, with your go-ahead.

---

## If you would rather do less

The minimum that still works: create **no** new fields, and build the sequence with only the existing **Personal Line** in step 1, with steps 2 and 3 as fixed copy. Say "minimum version" and I will adjust. It is weaker — the follow-ups stop being about them, which is the thing that seemed to earn your replies in March — but it still ships Monday.
