# Developer Task List — Lead Engine Roadmap v2 Implementation

**How to use this file:** work top to bottom. Tasks are ordered so each one is safe to ship on its own and so every change after Task 1 is *measurable*. Each task has: **what & why → files to touch → exact steps → acceptance test → effort**. Check the box when the acceptance test passes. Don't batch tasks — one commit per task, run `python -m pytest tests -q` before every commit.

**Branch:** work on `merge-lead-tool` (or branch from it). **Never** commit `.env`, `credentials.json`, `token.json`.

**Legend:** 🟢 cheap/low-risk · 🟡 medium · 🔴 large/needs its own scoping · ⏱ = rough dev time.

---

## GROUND RULES (read once)

- The scoring system prompt is in `scorer.py` → `SYSTEM_PROMPT`. The email prompt is in `emailer.py` → `EMAIL_PROMPT_TEMPLATE`. The verdict schema and guards are in `scorer.py`. Deterministic signals are in `scraper.py`. Taxonomy/thresholds are in `constants.py`. DB schema + migrations are in `db.py` (`_schema_sql()` for new tables, the `_add_column(...)` blocks in `init_db()` for new columns).
- After **any** prompt or threshold change, run the golden set (Task 1) and paste the agreement number in the commit message. This is non-negotiable — it's the whole point of doing Task 1 first.
- New DB columns: add them in `init_db()` via `_add_column(conn, "leads", "<col>", "<type>")` — it's idempotent and safe to re-run. New tables: add to `_schema_sql()`.
- Every new deterministic check must be **verified before it flags** (status code AND content shape) — a false positive is worse than a miss for anything that ends up in an email.

---

# PHASE 0 — Build the measuring instrument FIRST

> Rationale: Phase 1 edits the prompt and signals heavily. Without a regression harness you're back to "every prompt edit is superstition." Build this before touching anything else. (This is roadmap 4.2, pulled to the front deliberately.)

## ☐ Task 1 — Golden set + prompt regression harness 🟢 ⏱ 1 day

**What:** a fixed set of hand-verified leads with known-correct verdicts, re-scored on demand, reporting agreement so any prompt/threshold change is measured.

**Files:** new `golden/` folder, new `tools/run_golden.py`, new `tests/test_golden_smoke.py`.

**Steps:**
1. Create `golden/cases.jsonl` — one JSON object per line:
   ```json
   {"id":"roxie","expected_segment":"ai_solo_founder","expected_offer":"ai_audit","min_confidence":0.6,"notes":"Bubble app, parents upload kids' videos","fixture":"roxie.json"}
   ```
2. Create `golden/fixtures/<id>.json` for each case — the frozen scraper output (`rows`, `technical_signals`, `web_search_evidence`) so scoring runs **offline, no scraping, no web credits**. Capture these once from a real run (add a debug dump in `_process_lead`, or hand-build them).
3. Create `tools/run_golden.py`:
   - Loads each case + fixture, calls `scorer.score_content(...)` directly (pass a no-op `cost_cb`).
   - Compares `segment` (exact), `recommended_offer` (exact), `confidence ≥ min_confidence`, `needs_human_review` expectation.
   - Prints a table: per-case pass/fail, overall **agreement %**, a per-segment confusion matrix, and confidence calibration (avg confidence for correct vs incorrect).
   - Exit code non-zero if agreement < a threshold passed as `--min-agreement 0.8`.
4. Seed with **at least 15 cases now** (target 30–50): use the named leads in the roadmap — Roxie/Carlee/Dan/Marius/Hao/George/Eric/Sule (Bubble), plus the wasted-send examples Pauline/Thomas/Sascha/Shakeim as `budget_signal` negatives, plus a few clear `too_big`/`wrong_field`.
5. `tests/test_golden_smoke.py`: assert the harness runs and every fixture loads (doesn't need real API keys — mock `llm_provider.get_llm_provider` to return a canned verdict, OR mark the live-scoring run as an opt-in `@pytest.mark.live`).

**Acceptance test:** `python tools/run_golden.py --min-agreement 0.8` runs offline, prints the table, and returns non-zero when you deliberately break a case. `python -m pytest tests -q` still green.

**From here on:** every prompt/threshold task below ends with "run the golden set, record agreement in the commit."

---

# PHASE 1 — Corrections (ship before any new feature)

## ☐ Task 2 — Split builder fingerprints into APP vs SITE 🟢 ⏱ half day

**What & why:** `GENERATOR_FINGERPRINTS` currently mixes product-builders (Lovable/Bolt = STRONG "how the product was built") with site-builders (Framer/Webflow = "the marketing page is no-code", says nothing about the product). Campaign data: Bubble was the most-confirmed builder (8 leads) and is **missing today**; Framer/Webflow false-fired on 8 leads.

**Files:** `scraper.py` (`GENERATOR_FINGERPRINTS` → two dicts + logic in `extract_technical_signals` ~line 708), `scorer.py` (prompt), `db.py` (new signal columns), `export.py` (new columns).

**Steps:**
1. In `scraper.py` replace `GENERATOR_FINGERPRINTS` with:
   ```python
   APP_BUILDER_FINGERPRINTS = {   # STRONG — how the PRODUCT was built
     "lovable":  [r"lovable\.dev", r"lovable-tagger", r"gpteng\.co"],
     "bolt":     [r"bolt\.new", r"stackblitz"],
     "v0":       [r"v0\.dev", r"vusercontent\.net"],
     "replit":   [r"replit\.com", r"replit\.dev"],
     "bubble":   [r"bubble\.io", r"bubbleapps\.io"],
     "flutterflow": [r"flutterflow\.io", r"flutterflow\.app"],
     "glide":    [r"glideapps\.com", r"glide\.page"],
     "adalo":    [r"adalo\.com"],
     "softr":    [r"softr\.io"],
     "base44":   [r"base44\.app"],
     "cursor":   [r"built with cursor", r"cursor\.sh"],   # keep the special-rule caveat in the prompt
   }
   SITE_BUILDER_FINGERPRINTS = {  # METADATA ONLY — never a segment signal
     "framer":     [r"framer\.com", r"framerusercontent"],
     "webflow":    [r"webflow\.io", r"assets\.website-files\.com"],
     "squarespace":[r"squarespace\.com", r"static1\.squarespace"],
     "wix":        [r"wix\.com", r"wixstatic"],
     "carrd":      [r"carrd\.co"],
   }
   ```
2. In `extract_technical_signals`, emit two fields: `app_builder_fingerprint` (STRONG) and `site_builder_fingerprint` (metadata). Keep `generator_fingerprint` populated from `app_builder_fingerprint` for backward compat, OR do a clean rename and update every reader (grep `generator_fingerprint` across the repo first).
3. **Hosting-subdomain check** — near-proof of AI-build AND early stage. Add a helper that checks the lead's website host against `*.lovable.app, *.bolt.host, *.replit.app, *.bubbleapps.io, *.vercel.app` and emits `on_builder_subdomain: true` + which builder.
4. `db.py`: `_add_column(conn, "lead_technical_signals", "app_builder_fingerprint", "TEXT")`, same for `site_builder_fingerprint`, `on_builder_subdomain`. Update the INSERT in `save_lead_technical_signals`.
5. `scorer.py` prompt: in the STRONG bullet, say **app_builder_fingerprint** (not any builder) is the near-proof signal; add one line: "site_builder_fingerprint (Framer/Webflow/Wix/Squarespace/Carrd) is METADATA ONLY — it describes the marketing page's tooling, never how the product was built, and must NEVER move the segment." Add: "A product still served from a builder's default subdomain (on_builder_subdomain) is near-proof of both AI-build and early stage."
6. `export.py`: add the two columns to `SCRAPING_FIELDS`.

**Acceptance test:** a fixture whose HTML contains `bubbleapps.io` yields `app_builder_fingerprint="bubble"`; one with `framerusercontent` yields `site_builder_fingerprint="framer"` and empty `app_builder_fingerprint`. Golden set agreement ≥ baseline.

## ☐ Task 3 — Delete the visual-pattern detector (keep stat_banner) 🟢 ⏱ 2 hours

**What & why:** `purple_accent, gradient, glassmorphism, shadcn_ui, headline_badge, faq_accordion, numbered_steps, colored_glow` are 2026 web conventions used by every competent product. The prompt already forbids them from tipping a verdict — so they only add tokens and noise. `stat_banner` is a *traction* signal, not a build signal — keep it, renamed.

**Files:** `scraper.py` (`VISUAL_PATTERNS`, `extract_technical_signals`), `scorer.py` (prompt — remove the WEAK-signal defense paragraph), `db.py`/`export.py` (drop the column from new writes; leave the old column in place, just stop populating it).

**Steps:**
1. Remove `VISUAL_PATTERNS` and the `visual_patterns_triggered` extraction.
2. Add a tiny `TRACTION_PATTERNS = {"stat_banner": [r"\d[\d,\.]*\s?(?:\+|k\+)\s*(?:users|customers|clients)"]}` → emit `traction_signals: ["stat_banner"]` when it fires.
3. `scorer.py`: delete the entire "WEAK (never sufficient by itself): visual_patterns_triggered…" bullet and any other mention of visual patterns. (This is ~150 words gone — feeds Task 6.)
4. Leave the DB column `visual_patterns_triggered` (don't migrate-drop in Postgres); just stop writing it. New field `traction_signals` via `_add_column`.

**Acceptance test:** `extract_technical_signals` no longer returns `visual_patterns_triggered`; a fixture with "10k+ users" returns `traction_signals=["stat_banner"]`. Golden agreement ≥ baseline (should be unchanged or slightly better — less noise).

## ☐ Task 4 — Add `sensitive_data_categories` + `data_sensitivity_score` to the verdict 🟡 ⏱ half day

**What & why:** the roadmap's strongest empirical claim — **every hook that actually worked came from data sensitivity, not build detection** ("parents upload videos of their children", "credit applications and identity documents"). The taxonomy has no field for it today.

**Files:** `scorer.py` (schema in `SYSTEM_PROMPT` + `_validate_verdict` + `save_lead_score` mapping), `db.py` (columns + INSERT), `export.py` (columns), templates (`lead_review.html`, `results.html` optional).

**Steps:**
1. Add to the JSON schema block in `SYSTEM_PROMPT`:
   ```json
   "sensitive_data_categories": ["minors|health_phi|biometric|payments|identity_documents|financial|legal|location|employee_data|none"],
   "data_sensitivity_score": 0
   ```
2. Add a prompt instruction (concise): "Identify what categories of sensitive user data this product handles, based only on what the site/evidence states or clearly implies (e.g. a childcare app handles minors; a fintech handles financial + identity_documents). data_sensitivity_score 0–100 reflects how much a breach would hurt the product's users. Empty/none when there's no sensitive data. This is a primary relevance signal for the audit offer."
3. `scorer._validate_verdict`: coerce `sensitive_data_categories` to a list, drop values outside the allowed set, clamp score to 0–100, default `[]`/`0`.
4. `db.py`: `_add_column(conn, "lead_scores", "sensitive_data_categories", "TEXT")` (JSON) + `("data_sensitivity_score","INTEGER")`. Update `save_lead_score` (JSON-encode the list) and `get_leads_with_scores`/`get_lead_with_score` SELECTs.
5. `export.py`: add both to `SCORE_FIELDS`.
6. `lead_review.html`: show them on the card.

**Acceptance test:** a childcare-app fixture returns `sensitive_data_categories` containing `"minors"` and a non-zero score; the value round-trips through DB and appears in `scores.csv`. Golden agreement ≥ baseline.

## ☐ Task 5 — Add `budget_signal` to the verdict 🟡 ⏱ half day

**What & why:** the #1 wasted-send category — perfect persona, no wallet (nonprofit, student, side-project, shrinking headcount). No field expresses it today.

**Files:** same set as Task 4.

**Steps:**
1. Schema additions:
   ```json
   "budget_signal": "strong|moderate|weak|none",
   "budget_evidence": [],
   "budget_blockers": []
   ```
2. Prompt instruction (concise): "Assess the lead's ability to pay for a paid engagement. budget_evidence examples: visible paid pricing tiers, hiring multiple roles, raised funding, prior exit, enterprise logos. budget_blockers examples: nonprofit/donation-funded, student founder, side project alongside a full-time job, still on a default builder subdomain, headcount shrinking. A strong blocker caps budget_signal at 'weak' regardless of persona fit."
3. `_validate_verdict`: coerce enum + two lists.
4. DB columns `budget_signal TEXT`, `budget_evidence TEXT`, `budget_blockers TEXT`; wire through save/read/export/UI as in Task 4.
5. **Optional but recommended:** in `_categorize_leads` (app.py), leads with `budget_signal == "none"` AND a blocker present get a soft demote out of "Ready to approve" into "To review" with note "budget blocker" — so they don't silently reach the send queue.

**Acceptance test:** a nonprofit fixture returns `budget_signal` in {none, weak} with a blocker listed; round-trips to CSV. Golden agreement ≥ baseline.

## ☐ Task 6 — Cut the scorer prompt to < 1,200 words 🟡 ⏱ half day (do AFTER 2–5, LAST in Phase 1)

**What & why:** a ~2,500-word system prompt on a 70B open model is an instruction-following risk. Much of it is now redundant with code.

**Files:** `scorer.py` `SYSTEM_PROMPT`.

**Steps — remove (because they're enforced in code, not needed as prose):**
1. The 5 numbered STRUCTURAL DETECTION patterns — enforced by `_tag_attributed_content` (scraper) + `_verify_hooks_grounding` (scorer). Replace with one sentence: "Content wrapped in `[ATTRIBUTED QUOTE …]` / `[THIRD-PARTY CONTENT SECTION …]` markers is client/testimonial content — never attribute it to the analyzed company unless the attribution names the company's own founder. This is also enforced in code."
2. The visual-pattern WEAK defense (already deleted in Task 3).
3. Collapse the repeated first-party/third-party explanation to a single statement.
4. Keep intact: segment definitions, the STRONG/MEDIUM signal hierarchy (minus visual), the Cursor special rule, the metadata/title rule, the numbered decision procedure (9), the hook `based_on` rule (10) and the capability-inversion rule (11), and the JSON schema.

**Acceptance test:** `python -c "import scorer; print(len(scorer.SYSTEM_PROMPT.split()))"` prints < 1200. **Golden agreement must not drop** — if it does, you removed something load-bearing; restore the minimum that recovers it. Record before/after word count AND agreement in the commit.

## ☐ Task 7 — Switch scoring default to Claude 🟢 ⏱ 15 min + validation

**What & why:** at ~$0.02/lead the cost argument is gone; attribution/first-party/hierarchy reasoning is exactly where a frontier model beats a 70B. Groq stays as the Stage-0 bulk pre-filter (Task 14).

**Files:** `.env` (`SCORING_LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=…`), no code change (the switch already exists in `llm_provider.py`). `costlog.py` already has `claude-sonnet-4-6` pricing.

**Steps:** set the two env vars; restart. Optionally set `ANTHROPIC_MODEL` if you want a different Claude tier.

**Acceptance test:** run one real lead; `llm_calls` shows `provider=anthropic, model=claude-sonnet-4-6` and a non-zero `cost_usd`. **Re-run the golden set live (`@pytest.mark.live`) and compare agreement Groq vs Claude — keep whichever wins, and record both numbers.**

---

# PHASE 2 — The two additions that change what the product IS

## ☐ Task 8 — Public Surface Scanner 🔴 ⏱ 3–5 days (highest leverage, highest risk)

**What & why:** deterministic checks on **publicly visible** surface only, turning "you might have problems" into "here are two specific things visible on your site right now" — a real finding per lead before any relationship. This is the pull-through-a-push mechanism for a grudge purchase.

> ⚠️ **Before writing code:** get the go/no-go from Wael on the legal wording. Emailing a stranger "your /api returns data without auth" must be reviewed. Ship the *scanner* first (internal data only); do not put a finding into an outbound email until the template is approved.

**Files:** new `surface_scan.py`, `db.py` (new table), `pipeline.py` (call it during scrape), `lead_review.html` (show findings), later `emailer.py`.

**Steps:**
1. New table in `_schema_sql()`:
   ```sql
   CREATE TABLE IF NOT EXISTS lead_public_findings (
     {pk}, session_id INTEGER, lead_id INTEGER NOT NULL,
     check TEXT, severity TEXT, evidence_url TEXT, evidence_excerpt TEXT,
     verified INTEGER NOT NULL DEFAULT 0, verified_at TEXT);
   ```
2. `surface_scan.py` — implement these checks, **GET/HEAD only, single request to a well-known path, verify status AND content shape before flagging**:
   - `security_headers` — missing CSP / HSTS / X-Frame-Options / X-Content-Type-Options on the homepage response (low severity; informational).
   - `exposed_dotfiles` — `/.env`, `/.git/config`, `/.aws/credentials`: flag **only** on HTTP 200 AND content matching the expected shape (e.g. `.git/config` contains `[core]`; `.env` contains `KEY=` lines). A 404/redirect/HTML page is NOT an exposure. (This is the exact `cfood` false-positive to avoid.)
   - `framework_disclosure` — `X-Powered-By`, `Server`, `__NEXT_DATA__` version leakage (low).
   - `cors_wildcard` — `Access-Control-Allow-Origin: *` combined with `Access-Control-Allow-Credentials: true` (medium).
   - `source_maps_exposed` — a referenced `.map` returns 200 with a JSON source map (low/medium).
   - `open_api_endpoints` — only paths the site's own HTML/JS already reference; GET only; flag if it returns JSON data without auth (medium/high). **Never guess/fuzz paths.**
   - `public_llm_endpoint` — a chat widget calling an endpoint with no visible rate limit (informational; do NOT hammer it).
   - `storage_bucket_listing` — a referenced S3/GCS/Supabase URL returning a public index (medium).
   - Defer `known_vulnerable_deps` to a later iteration (needs a CVE feed).
3. Hard rules in code: allowlist of methods {GET, HEAD}; a per-domain politeness delay (reuse `site_fetcher`'s throttle); respect robots.txt for anything crawl-like; every finding row must have `verified=1` with an `evidence_excerpt` or it is not written.
4. `pipeline._process_lead`: after the scrape, call the scanner on the homepage host, persist findings, add a coverage note ("surface scan: N verified finding(s)"). Gate behind a config flag `[surface_scan] enabled` (default false until legal sign-off).
5. `lead_review.html`: a "Public findings" section (check, severity, evidence URL, excerpt).
6. **Re-verify at send time** (see Task 9 / email): a finding older than X days must be re-checked immediately before it goes into an email, or dropped.

**Acceptance test:** against a deliberately-exposed local fixture server, `/.git/config` returning real content is flagged; the same path returning a 404 or an HTML page is **not** flagged (regression-test both). No check ever issues a non-GET/HEAD request (assert in a unit test with a mocked requests layer).

## ☐ Task 9 — Trigger Monitoring (solve WHEN) 🔴 ⏱ 4–6 days

**What & why:** audits are bought at moments of pressure. Re-check scored leads on a schedule; when a readiness signal appears, the lead jumps to the top of the queue with the trigger as the hook. This is the direct answer to "same leads, right moment."

**Files:** new `triggers.py`, `db.py` (columns + table), a scheduled runner (cron or a loop), `results.html`/queue UI.

**Steps:**
1. Add `next_check_at TEXT` + `trigger_state TEXT` (JSON snapshot of last-seen values) to `leads`.
2. `triggers.py` — implement the cheap checks (weekly) and gate the expensive ones (LinkedIn, only for high-scoring leads):
   - `hiring_engineer` (careers page gains an engineering role — diff the careers signal), `pricing_introduced` (pricing page appears / free→paid), `compliance_page` (privacy/DPA/SOC2 appears), `custom_domain_move` (left a builder subdomain), `enterprise_logo` (new named customer), `app_store_launch` / `product_hunt` (search), `team_growth` (Apollo headcount crosses a threshold), `funding_announced`, `founder_posts_pain` (LinkedIn/X post — expensive, high-scorers only).
   - Each check compares against the stored `trigger_state` snapshot; a change fires an event.
3. New table `lead_trigger_events(lead_id, trigger, detected_at, detail)`.
4. Scheduler: a management command `python tools/run_triggers.py` (run by cron / a scheduled cloud job) that selects leads whose `next_check_at <= now`, runs the tier-appropriate checks, writes events, sets the next `next_check_at`.
5. Queue behavior: a fired trigger sets a high priority + provides the hook text (e.g. "Saw you're hiring your first engineer", "Saw you moved off the Lovable subdomain"). Surface these at the top of the review queue.

**Acceptance test:** given two stored snapshots of a careers page (before/after an engineering role is added), the checker emits a `hiring_engineer` event exactly once and updates the snapshot. Cheap checks never call the LinkedIn lane.

---

# PHASE 3 — Automation, Apollo, cost (makes it usable & cheap)

> These are larger; scope each as its own mini-spec when you reach it. Summaries + the acceptance bar only.

## ☐ Task 10 — Instantly/Smartlead export (do this early — it unblocks real sending) 🟡 ⏱ 1 day
Build a CSV export with `{{first_line}}` and the sending-tool custom variables (all lead fields + `segment, angle/offer, first_line, email_body, hooks_used`). Gate on `review_status='APPROVED'` (or the "Ready to approve" bucket) — **approval-gated, unlike today's `scores.csv`**. Record exported leads in `export_history`. **Acceptance:** an approved lead appears with a populated `first_line` column; a non-approved lead does not.

## ☐ Task 11 — Two-stage scoring (Stage 0 cheap pre-filter) 🟡 ⏱ 1–2 days
Add a Groq-based Stage-0 that scores from Apollo fields only (name/title/company/size/founded/employment history) at ~$0.0002/lead and kills obvious dev-shops/consultancies/enterprise/competitors **before** any scrape or enrichment credit is spent. Only survivors go to the full pipeline. **Validate the false-negative rate against the golden set before trusting it** (a cheap filter that kills good leads is expensive). **Acceptance:** Stage 0 rejects a known consultancy fixture and passes a known target fixture; golden-set good leads are never killed by Stage 0.

## ☐ Task 12 — Do-not-contact registry 🟢 ⏱ half day (do this early — safety)
First-class table of every email/domain ever contacted, checked automatically before any batch and before any send. **Acceptance:** a lead whose email is in the registry is blocked from the send queue with a visible reason. (This nearly caused a 22-person re-email — build it before scaling sends.)

## ☐ Task 13 — Apollo integration (in + out) 🔴 ⏱ 1–2 weeks
Wire the Apollo MCP tools: `apollo_mixed_people_api_search` (saved filter recipes, scheduled), `apollo_people_bulk_match` / `apollo_organizations_enrich` (enrich only post-pre-filter), `apollo_contacts_bulk_create` (push scored leads + hook custom field), `apollo_labels_*` (auto-list by segment), `apollo_emailer_campaigns_add` (enroll), `apollo_analytics_sync_report` (pull replies/opens nightly). Store **saved recipes** as versioned objects with historical yield. **Credit governor:** hard monthly budget, per-run estimate shown before spend, never enrich before Stage 0. **Acceptance:** a saved recipe runs, pre-filters, enriches only survivors, and the credit estimate matches actual spend within tolerance.

## ☐ Task 14 — Send-capacity planner 🟡 ⏱ 2–3 days
Own the calendar: inputs = mailboxes[], per-mailbox daily cap, warmup ramp, sequence touch count; outputs = 30-day sends/day forecast, "you can add N contacts on date D", follow-up-collision warnings, ramp schedule. Encode **every new contact books 3 slots (T1/T2/T3)**, so sustainable new-contact rate ≈ daily_cap ÷ 3. **Acceptance:** given 2 mailboxes at 40/day and a 3-touch sequence, the planner reports the correct sustainable new-contact/day number and flags a day where follow-ups exceed capacity.

## ☐ Task 15 — Three-click campaign flow 🔴 ⏱ 1 week
Recipe pick → keyboard-driven review queue (one card/lead: verdict, confidence, sensitive_data, budget_signal, public findings, hook+citation, **rendered email preview**; keys `A` approve / `X` reject / `E` edit hook / `→` next) → Ship (creates Apollo contacts, sets hooks, builds lists, enrolls, schedules by capacity). Background work stays inspectable, not operated. **Acceptance:** a reviewer can approve/reject 20 leads without leaving the keyboard, and "Ship" produces the Apollo artifacts.

---

# PHASE 4 — Analytics that compound

## ☐ Task 16 — Signal → Outcome attribution (the compounding one) 🔴 ⏱ 3–4 days
`lead_outcomes(lead_id, sent_at, opened, clicked, replied, reply_sentiment, meeting_booked, closed_won, revenue)`, populated nightly from `apollo_analytics_sync_report`. Screen: per-signal Leads / Sent / Replies / Reply-rate / **Lift** table (as in roadmap 4.1). **This is the only feature that makes the system get smarter — after ~500 sends it tells you what to target and what to delete.** **Acceptance:** the table renders real per-signal reply rates once outcomes are populated; lift is computed vs the overall baseline.

## ☐ Task 17 — Cost per outcome 🟡 ⏱ 2 days
Not cost/lead — cost per positive reply and per closed deal, split by recipe/segment/channel, including Apollo credits + LLM (`llm_calls` already logs this) + scraping + time. **Acceptance:** given outcomes + `llm_calls` + Apollo spend, the screen shows $/reply and $/close per recipe.

## ☐ Task 18 — Channel comparison 🟡 ⏱ 2 days
Cold email vs Upwork vs Discord vs inbound on the same axes (leads, cost, replies, closes, revenue, cycle length). This screen exists to answer "should cold email continue at all?" honestly. **Acceptance:** all channels render on one comparable table.

---

# PHASE 5 — High ceiling, needs the base

- ☐ **Task 19 — Reply intelligence** 🔴: classify inbound (interested/objection/not-now/wrong-person/unsubscribe), draft a contextual response, one-click send. Speed-to-reply is the biggest conversion lever. **Do this.**
- ☐ **Task 20 — Multi-channel intent ingestion** 🔴 (highest ROI of Phase 5): Upwork job monitoring for audit-intent keywords (already produced 4 clients), Discord/community watching (run the surface scan on live URLs founders post asking for feedback), Product Hunt / App Store launch watching. Same scoring engine, warmer entry. **Strongly consider pulling parts of this earlier — it points at the channel that actually converts.**
- ☐ **Task 21 — Evidence Pack** 🟡: 1-page anonymised mini-report from the surface scan, sent as the reply to "yes, tell me more." Converts a claim into a deliverable. **Do this (after Task 8).**
- ☐ **Task 22 — Auto-experiment framework** 🟡: multi-armed email-variant testing with significance gating (refuses to call a winner at n=20). **Later — needs volume.**
- ☐ **Task 23 — Self-serve scanner lead magnet** 🔴: public "paste your URL, get the scan" page. **Only after Task 8's verification is bulletproof** — false positives are an un-undoable credibility loss (the `cfood` cautionary tale).

---

# Suggested delivery order (fastest safe path)

**Week 1:** Task 1 (golden set) → Task 12 (do-not-contact) → Task 7 (Claude default, validate) → Task 2 (fingerprint split).
**Week 2:** Task 3 (delete visual) → Task 4 (sensitive data) → Task 5 (budget) → Task 6 (prompt diet). *End of Phase 1 — re-run golden set, record the agreement delta across all of Phase 1.*
**Week 3–4:** Task 10 (Instantly export) → Task 8 (surface scanner, pending legal OK) → Task 11 (Stage 0).
**Week 5+:** Task 9 (triggers), then Phase 3/4 by business priority. Start Task 16 (signal→outcome) data capture as early as the first real sends happen — it needs history to be useful.

**If only three things ever ship:** Task 8 (surface scanner), Task 16 (signal→outcome), Task 1 (golden set). Those three turn a qualification tool into a system that produces evidence, learns from results, and improves deliberately.

---

# Definition of done (every task)
1. Acceptance test passes.
2. `python -m pytest tests -q` green.
3. For any prompt/threshold/signal change: golden-set agreement recorded in the commit message (and not below the last baseline without a written reason).
4. One task per commit, clear message, no secrets committed.
5. New behavior is behind a config flag when it changes spend or sends.
