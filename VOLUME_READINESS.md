# Volume Readiness — 200-lead batches

This documents the volume layer added so the system can **source, filter and prepare 200+ leads per batch without manual sourcing**, and stay inside the Apollo credit budget.

## What was added

| Piece | File | What it does |
|---|---|---|
| **Apollo sourcing** | `apollo_client.py`, `sourcing.py` | Pull leads directly from the Apollo API — search (free) → Stage-0 pre-filter (free) → credit-gated enrich (1 credit/survivor) → insert into a session. No human CSV export. |
| **Stage-0 pre-filter** | `prefilter.py` | Deterministic rules on FREE Apollo fields (title/company/headcount) drop obvious non-fits — agencies, consultancies, fractional CTOs, enterprise, non-decision-makers — **before any credit or fetch is spent.** Optional cheap Groq pass for ambiguous cases. Conservative: when unsure it KEEPS, never rejects. |
| **Apollo credit governor** | `apollo_client.py` (`apollo_usage` table) | Persistent monthly credit counter. Blocks any enrichment that would exceed `[apollo].monthly_credit_cap` (default 3600) **before** the call. Records actual usage after. |
| **Saved recipes + yield** | `recipes.py` (`apollo_recipes` table) | Named, reusable Apollo searches, each tracking runs / pulled / qualified / enriched / sent / replies → the UI shows "this recipe yields 85% qualified but 0% replies". Supports your validated finding: run MANY narrow vertical searches, not a few broad ones. |
| **Do-not-contact registry** | `dnc.py` (`do_not_contact` table) | Email + domain, permanent, checked **on import** (not just export). Auto-populated on every Instantly export and every Gmail send. The fix for the near-miss 22-person re-email. |
| **Instantly/Smartlead export** | `export.py` (`instantly_csv_string`), `/download/instantly.csv` | Approval-gated CSV with `{{first_line}}` as a custom variable. Only exports leads in "Ready to approve" (or explicitly APPROVED); records every exported lead in the DNC registry. |
| **Bulk approve** | `/session/<id>/bulk_approve` | Approve many leads at once, or "approve all to-review leads ≥ confidence X" in one click — the volume review path. |
| **Sourcing UI** | `templates/sourcing.html`, `/sourcing` | Save recipes, dry-run (see how many would enrich, 0 credits), or run (spend credits). Shows monthly credit position. |

## The order that is enforced (never spend before you filter)

```
search_people_all        FREE   (paginated, capped at [apollo].max_people_per_run)
  → prefilter_people      FREE   (Stage-0 drops non-fits)
  → DNC check on survivors FREE  (never enrich someone we can't contact)
  → check_credit_budget   FREE   (raises if over monthly cap)
  → enrich_people         1 CREDIT / survivor
  → insert leads + flag DNC on import
  → record recipe yield
```
A **dry run** stops after the pre-filter and reports what *would* be enriched — zero credits.

## New config (`config.toml`)

```toml
[apollo]
monthly_credit_cap = 3600   # hard monthly enrichment cap; 0 = off
search_page_size = 100      # Apollo max
max_people_per_run = 500    # safety ceiling per recipe run

[prefilter]
enabled = true
use_llm = false             # true = cheap Groq pass for "unclear" leads
max_headcount = 50          # reject above this (too big); 0 = no bound
min_headcount = 0
```

## New `.env`

```
APOLLO_API_KEY=...          # required for live Apollo calls
```

## How to run a 200-lead batch now

1. `/sourcing` → save a recipe (narrow vertical, e.g. `{"person_titles":["founder","co-founder","ceo"],"q_keywords":"health app","organization_num_employees_ranges":["1,10","11,20"]}`).
2. **Dry run** it → see "pulled 170, would enrich 60 (60 credits)". Zero spend.
3. **Run** → search + filter + enrich + insert into a session (credits recorded, capped).
4. Open the session → start the analysis pipeline (scrape + score) as usual.
5. Results page → **Bulk approve** the high-confidence targets → **Export to Instantly/Smartlead**.
6. Every exported/sent lead is now on the do-not-contact registry; the next import skips them automatically.

## What is deliberately NOT changed (and why)

- **LinkedIn deep-harvest stays sequential + capped (50/day, 250/week).** At 200 leads it degrades gracefully to snippet search past the cap and writes a coverage note — it does **not** block the batch. For 200-lead throughput, expect deep LinkedIn evidence only on the top slice; run it overnight for more. This is a capacity decision, not a bug.
- **Scrape+score concurrency stays at 3** (`PIPELINE_CONCURRENCY`). Per-lead status is persisted, so a 200-batch survives a crash and resumes without rescoring done leads.
- **Review UI is not yet paginated.** Bulk-approve removes most of the per-lead clicking; the keyboard review queue (roadmap Task 15) is the next UX step for 200-at-a-time.

## Tests

21 new offline tests (no API key, no DB server): `prefilter` rules, `dnc` registry + import flagging, Apollo credit governor (blocks over-cap before any HTTP call), Instantly export gating + `{{first_line}}`. Plus a mocked end-to-end sourcing run. Total suite: **51 tests, all green** (`python -m pytest tests -q`).

## Honest status

- **Sourcing, pre-filter, credit governor, DNC, Instantly export, bulk approve: built and tested offline.** Live Apollo calls need `APOLLO_API_KEY` and a real-world shakeout (field names in the Apollo response can vary by plan — `apollo_client.person_to_lead_row` and `_person_match_key` are the two places to adjust if a field comes back under a different key).
- **This makes 200 affordable and unattended to source.** The remaining human bottleneck is review; bulk-approve mitigates it, the keyboard queue removes it.
