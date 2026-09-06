# Lead Engine — Status Report (where we are right now)

**Date:** 6 September 2026 · **Branch:** `merge-lead-tool` (pushed) · **Audience:** external reviewer (Claude web) who already has the persona, the original cahier des charges, `SYSTEM_OVERVIEW.md` and `lead-engine-roadmap-v2.md`.

This document is the delta since `SYSTEM_OVERVIEW.md`: what was built, what was wired live, what the first real runs showed, what broke, what was fixed, and what is open. Every number below comes from a live run today, not from a mock. No secrets are included.

---

## 1. Infrastructure — what is actually running

| Layer | State |
|---|---|
| **Code** | GitHub `oussama121tt/Lead-Qualification`, branch `merge-lead-tool`, latest commit `b0cde76`. 91 offline tests, all green (`python -m pytest tests -q`). |
| **Database** | **Supabase Postgres** (project ref `ltvjoxgysxcojrwhbssz`). Connected via the **Session pooler** host `aws-1-eu-west-1.pooler.supabase.com:5432`, user `postgres.<ref>`, `sslmode=require`. The **Direct** host (`db.<ref>.supabase.co`) is IPv6-only and does not resolve from the dev machine — the pooler is the working path. Schema initialised by `db.init_db()`: 14 tables (`analysis_sessions, users, leads, lead_content, lead_technical_signals, lead_scores, lead_search_evidence, export_history, llm_calls, li_daily_counter, apollo_usage, apollo_recipes, do_not_contact, lead_public_findings`). |
| **Deployment** | The developer has deployed the app to **Render** (gunicorn, `gunicorn.conf.py` added, worker-timeout and schema-migration fixes committed). Local dev still runs with `python app.py`. |
| **LLM** | Scoring + emails via **Groq**, model **`openai/gpt-oss-120b`** (the developer switched from llama-3.3-70b). Claude (`claude-sonnet-4-6`) switchable per flow via `SCORING_LLM_PROVIDER` / `EMAIL_LLM_PROVIDER=anthropic` — not enabled (no `ANTHROPIC_API_KEY` set). |
| **Scraping** | Free-first (requests+BeautifulSoup); **Firecrawl** as paid fallback (1 key configured). |
| **Web/LinkedIn evidence** | **ScrapeGraphAI (SGAI)** — 1 key configured and **now OUT OF CREDITS** (exhausted during today's first batch). The escalation lane degrades gracefully to "no web evidence" with a coverage note, but deep LinkedIn evidence is unavailable until credits are topped up or `SGAI_API_KEY_2` is added. |
| **Apollo** | **Master API key, live.** People search via `POST /api/v1/mixed_people/api_search` (see §3). Credit governor active: **29 of 3,600** monthly credits used. |
| **Sending** | Gmail OAuth (`setup_gmail.py`) not yet run on this machine; Instantly/Smartlead export built (§2). Nothing has been sent from the merged system. |

`.env` variables present (values withheld): `DATABASE_URL`, `APOLLO_API_KEY`, `GROQ_API_KEY`, `FIRECRAWL_API_KEY`, `SGAI_API_KEY`. Not set: `ANTHROPIC_API_KEY`, `FLASK_SECRET_KEY` (random per process), `SENDER_NAME`.

---

## 2. What was built since SYSTEM_OVERVIEW.md

### 2.1 Developer's work (Phase 1 of roadmap v2 + scanner) — 12 commits
- **Task 1 — Golden set + regression harness**: `golden/cases.jsonl` (15 cases), `tools/run_golden.py` (offline, mocked LLM), `tools/run_golden_live.py` (real LLM). Reported 100% agreement on the mock.
- **Task 2 — Fingerprint split**: `APP_BUILDER_FINGERPRINTS` (Lovable/Bolt/v0/Replit/Bubble/FlutterFlow/Glide/Adalo/Softr/Base44/Cursor) vs `SITE_BUILDER_FINGERPRINTS` (Framer/Webflow/Squarespace/Wix/Carrd, metadata only) + `on_builder_subdomain` check. Test `test_builder_subdomain.py`.
- **Task 3 — Visual-pattern detector removed** (stat_banner kept as a traction signal).
- **Task 4 — `sensitive_data_categories` + `data_sensitivity_score`** in the verdict schema, DB, export, review page.
- **Task 5 — `budget_signal` / `budget_evidence` / `budget_blockers`**; leads with `none` + a blocker are demoted out of "Ready to approve".
- **Task 6 — Prompt diet**: `SYSTEM_PROMPT` cut from ~2,500 to **330 words**. (This introduced a regression — see §4.)
- **Task 8 — Public Surface Scanner** (`surface_scan.py`, 386 lines, 8 GET/HEAD-only checks, per-check isolation, body caps, verification-before-flag; `lead_public_findings` table; **enabled** in config). Findings are shown on the lead review page as "internal review only". No finding is put into an email.
- Render deployment fixes; session-status fixes.

### 2.2 My work — the volume layer (roadmap Phase 3 blockers) — verified live today
| Module | What it does | Live status |
|---|---|---|
| `apollo_client.py` | Apollo REST: people search (free) + bulk enrich (1 credit/person), monthly **credit governor** (`apollo_usage` table, cap 3,600, checked BEFORE the call) | ✅ live |
| `prefilter.py` | **Stage-0** deterministic filter on free search fields: rejects `has_email=false`, agencies/consultancies/dev shops, fractional CTOs, non-decision-makers, out-of-range headcount; optional Groq pass for "unclear"; conservative (keeps when unsure) | ✅ live |
| `sourcing.py` | Enforced order **search → prefilter → DNC → credit-gate → enrich → insert**; dry-run mode spends 0 credits; injects `contact_email_status=["verified"]` into every search | ✅ live |
| `recipes.py` | Saved Apollo searches with yield counters (runs/pulled/qualified/enriched/sent/replies) | ✅ live (1 recipe) |
| `dnc.py` | **Do-not-contact registry** (email + domain), checked **on import**, auto-populated on every export and send | ✅ wired (0 rows yet — nothing sent) |
| `export.py` `instantly_csv_string` | Approval-gated Instantly/Smartlead CSV with `{{first_line}}` | ✅ built |
| `/session/<id>/bulk_approve` | Bulk approve, or "approve all ≥ confidence X" | ✅ built |
| `/sourcing` UI | Save recipe, dry run, run, credit position | ✅ built |
| Enrichment capture | `apollo_email_status`, `apollo_person` (seniority, headline, location, **employment history**), `apollo_org` (headcount, founded, industry, headcount growth, revenue, keywords) stored on the lead and **rendered into the scorer's metadata block** | ✅ live |

---

## 3. Apollo integration — what the live API taught us

1. **Endpoint**: `POST /mixed_people/search` returns `403 API_INACCESSIBLE` for *every* API key, master included — it is UI-session-only now. API keys must use **`/mixed_people/api_search`**. Fixed.
2. **Search results are heavily obfuscated**: only `id`, `first_name`, `last_name_obfuscated`, `title`, `organization.name`, `has_email/has_*` flags. **No headcount, domain, or founded year until enrichment.** Consequences: the headcount bound must live in the recipe filter (`organization_num_employees_ranges`), the pre-filter works on title + company name, and DNC-by-domain cannot run before enrichment (email-level DNC runs on import, after).
3. **Enrichment is rich**: `email`, `email_status` (verified/guessed/unavailable), `linkedin_url`, `seniority`, `headline`, `employment_history` (title/org/dates/current — up to 11 entries seen), and full `organization` (domain, website, employees, founded_year, industry, 6/12/24-month headcount growth, revenue, keywords, LinkedIn).
4. **Credits**: enrichment charges per matched person; re-matching a person already revealed for the team (`revealed_for_current_team: true`) costs 0. `bulk_match` max 10 per call (code chunks accordingly).
5. **The biggest finding: 52% waste without a verified-email filter.** First run: 21 people enriched, **11 had `email_status: unavailable`** — no email exists at Apollo, not even personal. `contact_email_status=["verified"]` at search time cut the same pool from 21 to **8, all verified**, for free. Now default.

**Recipe used:** `{"person_titles":["founder","co-founder","ceo"], "q_keywords":"health app", "organization_num_employees_ranges":["1,10","11,20"]}` → 1,439 people on keyword alone; 21 with titles+headcount; **8 with verified emails**. Consistent with the manual finding that narrow verticals give small, high-quality pools.

---

## 4. First end-to-end batch — what happened, honestly

**Session 1: 8 verified health-app founders**, scraped + scored + escalated, then re-scored after fixes.

| # | Company | Site fetch | Verdict (final) | Conf | Hooks | Sensitive | Budget |
|---|---|---|---|---|---|---|---|
| 1 | Halo Health App | dead domain (DNS) | technical_founder, review | 0.0* | 0 | — | — |
| 2 | Doctor2U | Firecrawl timeout | technical_founder, review | 0.0* | 0 | — | — |
| 3 | Snore Free | free fetch OK | unclear, review | 0.60 | 0–1 | health_phi 70 | weak |
| 4 | Namaste Health App | OK | unclear, review | 0.85 | 2 | health_phi | none |
| 5 | Solvi Health App | OK | unclear, review | 0.60 | 1 | health_phi | none |
| 6 | Smash Health app | OK | unclear | 0.82 | 2 | health_phi | none |
| 7 | Poka health app | OK | **technical_founder → general_audit**, review | 0.92 | 0 | health_phi | none |
| 8 | Aria Health | OK | **technical_founder → general_audit**, review | 0.78 | 2 | health_phi | none |

\* leads 1–2 have no site content and were scored on metadata + web evidence only (correctly forced to review); they were not re-scored because the rescore path requires stored content.

**Cost of the whole session:** 22 LLM calls, 92,771 tokens in / 21,630 out, **$0.011**. Apollo: 29 credits total (21 first run + 8 clean run). Time: ~14 min for the first full run of 8 (Firecrawl timeouts on 2 dead/slow sites, LinkedIn harvest), ~35 s/lead on re-score.

**Quality read:** `health_phi` detected on every health app (correct); the two technical-founder calls are consistent with their career histories (ex-IT Director, engineering backgrounds); the four `unclear` verdicts are honest — thin marketing sites with no build signal either way. No `ai_solo_founder` in this pool, which is plausible for established health apps. Hooks that survived carry verbatim citations (e.g. *"Finally, sleep through again."*).

### 4.1 The regression the batch exposed (fixed today)
The first scoring pass produced **confidence 0.0 and empty hooks/quotes/signals for every lead**. Root cause: the 330-word prompt diet had **removed the JSON schema block**; `gpt-oss-120b` then answered with its own key names (`hooks`, `offer`, no `evidence_quotes`, no signal lists). The parser read the canonical names, found nothing, and the guards zeroed the verdict. The offline golden harness **mocks the LLM**, so it reported 100% while production was broken; the live golden runner had not been run.

**Fixes (commit `b0cde76`):** schema block restored at the end of the prompt (now ~450 words); `_normalize_verdict_keys` alias map applied after every call, with the drift recorded in `disqualify_reason`; `tests/test_prompt_schema_contract.py` asserts the prompt names every schema key and stays < 1,200 words; `scorer._normalize_for_grounding` now strips wrapping quotation marks and folds curly quotes/nbsp (the model returns citations as `"\"exact text\""` — real hooks were being discarded as ungrounded); `unclear` now forces `needs_human_review` in code; `openai/gpt-oss-120b` priced in `costlog`.

### 4.2 Operational facts from the run
- Free-first fetching worked on 6/8; 2 escalated to Firecrawl (1 dead domain, 1 timeout).
- Surface scanner ran on every lead: 5 findings stored, all "internal review only".
- LinkedIn lane harvested profiles until **SGAI credits ran out mid-batch** (`SGAI key 1 out of credits`); subsequent leads got "no web evidence" coverage notes instead of failing.
- Junk-post filtering dropped 1–7 auth-wall/blank posts per profile.

---

## 5. Open items (blunt)

**Must do before a real send campaign**
1. **SGAI credits** — top up or add a second key; without it the escalation lane produces no LinkedIn/web evidence.
2. **Run the LIVE golden set** (`tools/run_golden_live.py`) against the fixed prompt and **grow it to 30–50 hand-verified cases**. 15 mocked cases just proved they cannot catch a real regression.
3. **Sending channel**: Gmail OAuth not set up here; recommended path is the Instantly/Smartlead export + warmed mailboxes. Decide and configure.
4. **Legal wording review** for any email that cites a surface-scan finding (scanner is on; findings are internal-only until this is done).

**Should do soon**
5. Verified-email pools are small (8 here). Volume will come from **many narrow recipes**, not bigger ones — save 10–20 verticals and run them on a schedule (recipe queue/scheduler not built yet).
6. `apollo_person`/`apollo_org` are now in the prompt; **budget_signal should also read `headcount_growth`, `revenue`, `founded_year`** explicitly (currently the model sees them as free text).
7. Reply/outcome capture (roadmap 4.1) — nothing feeds `sent`/`replies` on recipes yet. This is the loop-closer.
8. Review UI pagination / keyboard queue for 200-lead sessions (bulk approve exists; per-lead review is still click-heavy).
9. Rescore path cannot re-score leads without stored site content (leads 1–2) — should fall back to metadata + web evidence like the main pass.

**Known limits (by design)**
- LinkedIn lane: sequential, 50/day / 250/week global caps, human pacing — deep evidence only on a slice of a 200 batch.
- Dead/slow sites cost time (Firecrawl timeouts ~2 min); consider a shorter Firecrawl timeout for the fallback path.

---

## 6. Readiness verdict

- **Sourcing → filtering → enrichment → scoring → review → export: works end to end on real data, with real cost/credit controls.** A 50–100 lead/week pilot across several narrow recipes, with human review, exporting to Instantly, is safe to start now.
- **Not yet safe for unattended 200/batch or 600/month**: SGAI credits, live golden validation of the new prompt, sending infrastructure, and reply capture are the gaps.
- The thesis (readiness + evidence beats better targeting) remains **unproven until replies exist** — item 7 above is what turns this from a well-built machine into a measurable one.

**Questions for the reviewer**
1. Are the four `unclear` verdicts on thin health-app sites the right honest outcome, or should the scorer lean on employment history harder to commit to `technical_founder` / `ai_solo_founder`?
2. With verified-email pools this small (8 per narrow recipe), is 600 contacts/month realistic through Apollo alone, or should Upwork/Discord intent ingestion (roadmap 5.2) move up?
3. Should the pre-filter's headcount cap (50) be lowered for the audit offer, given `employees=1–12` across this pool?
4. Is `health_phi` on every health app a useful hook driver, or noise for this vertical — should sensitivity only count when the site states data handling explicitly?
