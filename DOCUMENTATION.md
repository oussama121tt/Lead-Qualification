# Lead Qualification & Scoring Engine — Complete Technical Documentation

> Ultra-detailed version of the project. This document describes the full architecture, database schema, routes, algorithms, and application behaviors, with every fact referenced to its source (`file.py:line`).
>
> **Scope**: CSV ingestion → deduplication → web scraping (Firecrawl) → AI scoring (Groq) → human review → exports. Flask web interface.

---

## Table of contents

1. [Overview](#1-overview)
2. [Architecture & data flow](#2-architecture--data-flow)
3. [Installation & configuration](#3-installation--configuration)
4. [Startup](#4-startup)
5. [PostgreSQL data model](#5-postgresql-data-model)
6. [Authentication & roles](#6-authentication--roles)
7. [Flask routes (32 routes)](#7-flask-routes-32-routes)
8. [CSV ingestion](#8-csv-ingestion)
9. [Deduplication](#9-deduplication)
10. [Firecrawl scraping](#10-firecrawl-scraping)
11. [Web escalation (ScrapeGraphAI)](#11-web-escalation-scrapegraphai)
12. [Deterministic technical signals](#12-deterministic-technical-signals)
13. [Groq scoring](#13-groq-scoring)
14. [Orchestrator pipeline](#14-orchestrator-pipeline)
15. [Real-time progress (SSE)](#15-real-time-progress-sse)
16. [Exports](#16-exports)
17. [Segments, statuses & state machine](#17-segments-statuses--state-machine)
18. [Sequence housekeeping](#18-sequence-housekeeping)
19. [Templates & UI](#19-templates--ui)
20. [Environment variables](#20-environment-variables)
21. [Dependencies & files](#21-dependencies--files)
22. [Consistency notes & known pitfalls](#22-consistency-notes--known-pitfalls)

---

## 1. Overview

Full pipeline: **Ingestion → Deduplication → Scraping (Firecrawl) → AI Scoring (Groq)**, with a Flask web interface and **PostgreSQL (Neon only** — no SQLite fallback, `db.py:1-14`).

**Marketing target**: non-technical founders who build with AI (vibe coding, Cursor, Bolt, Lovable, Replit) — `ai_solo_founder` segment, `ai_audit` recommended offer. The application qualifies each B2B lead into 6 segments and produces a structured JSON verdict (segment, confidence, offer, quotes, personalization hooks, disqualification reason).

**Stack**:

| Component | Technology |
|---|---|
| Web backend | Flask (Python 3.11) |
| Database | PostgreSQL on Neon (`DATABASE_URL`), psycopg2 pool |
| Scraping | Firecrawl (multi API keys, 1 thread/key parallelism) |
| Web escalation | ScrapeGraphAI (search + LinkedIn full scrape) |
| AI scoring | Groq — `llama-3.3-70b-versatile` (OpenAI SDK, `base_url="https://api.groq.com/openai/v1"`) |
| Frontend | Bootstrap 5.3.3 (dark theme), SSE for live progress |

---

## 2. Architecture & data flow

### 2.1 Processing chain (user view)

```
1. Apollo CSV import (POST /upload)             → ingestion + dedup
2. Import review       (GET /import/<session_id>) → criteria selection, duplicate re-inclusion
3. Pipeline            (POST /import/<session_id>/start) → scraping + scoring (background thread)
4. Progress            (GET /progress/<session_id>) → real-time SSE
5. Results             (GET /results/<session_id>) → 5 categories, human review
6. Exports             (CSV scraping / scores / search / complete CSV or PDF)
```

### 2.2 Modules and responsibilities

| File | Role |
|---|---|
| `app.py` | Flask interface: routes, auth, background threads, SSE progress, result categorization |
| `db.py` | PostgreSQL schema + CRUD helpers, connection pool, sequence housekeeping |
| `constants.py` | Single source of truth: segments, statuses, confidence threshold |
| `dedup.py` | 3 deduplication levels + export-history check |
| `scraper.py` | Firecrawl scraping, anti-fake-page filters, technical signals, GitHub API, SGAI escalation |
| `scorer.py` | Groq scoring: structured system prompt, JSON verdict, guards and retries |
| `pipeline.py` | Orchestrator: chains scraping + scoring lead by lead, isolates errors |
| `export.py` | 4 CSV formats + readable summary, CLI, cross-batch dedup |

### 2.3 Database connections

- **Pool**: `psycopg2.pool.ThreadedConnectionPool`, min `DB_POOL_MINCONN` (default 1), max `DB_POOL_MAXCONN` (default 8) — `db.py:39-40, 60-61`.
- **TCP keepalives**: `keepalives=1, keepalives_idle=60, keepalives_interval=15, keepalives_count=4` — `db.py:233-236`.
- **Reconnection**: 5 attempts spaced `2*(attempt+1)` s — `db.py:227-241`; an `execute` retries once after reconnecting on a dead connection (10 detected error patterns: "could not receive data from server", "software caused connection abort", "server closed the connection", "connection reset by peer", "ssl syscall error", "broken pipe", "connection has been closed", "terminated by server", "no connection to the server", "connection refused" — `db.py:43-59, 144-153`).
- **Saturated pool fallback**: `PoolError` → direct `psycopg2.connect(DATABASE_URL)` — `db.py:260-263`.
- **Per lead**: the pipeline opens a dedicated connection per lead, closed in a `finally` — `pipeline.py:131, 270-274`.
- **Wrappers**: `_PgConnection` (execute/executemany/executescript — split on `;`, `db.py:184-186`, commit/rollback), `_PgCursor`, `_PgRow` (accessible by key AND by index) — `db.py:105-210`.
- Timestamps: `datetime.now(timezone.utc).isoformat(timespec="seconds")` — `db.py:267-268`.

---

## 3. Installation & configuration

```bash
pip install -r requirements.txt
```

Create `.env` at the repo root (see [§20](#20-environment-variables) for the exhaustive list):

```env
DATABASE_URL=postgresql://user:password@host/dbname
FIRECRAWL_API_KEY=...
FIRECRAWL_API_KEY_2=...        # optional, up to _5
GROQ_API_KEY=...
SGAI_API_KEY=...               # optional, up to _5
```

> ⚠️ The current README contains an unclosed ```bash block in its Installation section, which breaks the Markdown rendering downstream (to fix).

---

## 4. Startup

```bash
python app.py
```

- `_init_schema_once()` called **once** at startup (`__main__`, `app.py:1325`): creates the schema, adds missing columns, creates the users index, applies the sequence housekeeping.
- Server: `app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=True, use_reloader=False)` — `app.py:1324-1326`.
- Without `DATABASE_URL` → RuntimeError at startup (`db.py:217-221`).
- Without `FIRECRAWL_API_KEY` or `GROQ_API_KEY` → flash message on the interface (`app.py:287-288`).

---

## 5. PostgreSQL data model

Common PK: `id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY` — `db.py:500`.

### 5.1 `analysis_sessions`

| Column | Type | Constraints |
|---|---|---|
| id | BIGINT | IDENTITY PK |
| label | TEXT | |
| source_filename | TEXT | |
| status | TEXT | NOT NULL DEFAULT 'imported' |
| created_at | TEXT | NOT NULL |
| completed_at | TEXT | |
| notes | TEXT | |
| owner_id | INTEGER | (added retroactively — `db.py:724`) |
| cancelled | INTEGER | NOT NULL DEFAULT 0 |
| scoring_criteria | TEXT | (JSON list) |
| scoring_criteria_custom | TEXT | |
| last_batch_ids | TEXT | (JSON list) |

### 5.2 `users`

| Column | Type | Constraints |
|---|---|---|
| id | BIGINT | IDENTITY PK |
| email | TEXT | NOT NULL UNIQUE (index `idx_users_email` created in `init_db`) |
| password_hash | TEXT | NOT NULL |
| role | TEXT | NOT NULL DEFAULT 'user' |
| is_active | INTEGER | NOT NULL DEFAULT 1 |
| created_at | TEXT | NOT NULL |
| last_login_at | TEXT | |

### 5.3 `leads`

| Column | Type | Constraints |
|---|---|---|
| id | BIGINT | IDENTITY PK |
| session_id | INTEGER | FK → analysis_sessions(id) |
| first_name / last_name / title / company_name / email / website_url | TEXT | |
| domain_normalized | TEXT | |
| email_domain | TEXT | |
| domain_mismatch | INTEGER | NOT NULL DEFAULT 0 |
| domain_mismatch_reason | TEXT | |
| status | TEXT | NOT NULL DEFAULT 'NEW' |
| is_duplicate | INTEGER | NOT NULL DEFAULT 0 |
| duplicate_of_id | INTEGER | FK → leads(id) |
| duplicate_reason | TEXT | |
| batch_id | TEXT | |
| created_at | TEXT | NOT NULL |
| review_status | TEXT | (retroactive) |
| review_segment_override | TEXT | (retroactive) |
| reviewed_at | TEXT | (retroactive) |
| last_error | TEXT | (retroactive) |
| scrape_seconds | REAL | (retroactive) |
| score_seconds | REAL | (retroactive) |

### 5.4 `lead_content`

id PK, `session_id` INTEGER FK, `lead_id` INTEGER **NOT NULL** FK → leads(id), `source` TEXT, `url` TEXT, `content` TEXT, `fetched_at` TEXT NOT NULL.

### 5.5 `lead_technical_signals`

id PK, `session_id` INTEGER FK, `lead_id` INTEGER NOT NULL FK, `generator_fingerprint` TEXT, `vibe_language_matches` TEXT, `trend_fonts_found` TEXT, `visual_patterns_triggered` TEXT, `generator_meta_tag` TEXT, `github_repo_url` TEXT, `github_check` TEXT, `ai_style_phrases_found` TEXT, `ai_style_phrase_density` TEXT, `ai_authorship_disclosures_found` TEXT, `computed_at` TEXT NOT NULL. (JSON-serialized lists — `db.py:1045-1112`.)

### 5.6 `lead_scores`

id PK, `session_id` INTEGER FK, `lead_id` INTEGER NOT NULL FK, `segment` TEXT, `confidence` DOUBLE PRECISION, `company_stage` TEXT, `built_with_ai_signals` TEXT, `technical_signals` TEXT, `pain_signals` TEXT, `evidence_quotes` TEXT, `recommended_offer` TEXT, `personalization_hooks` TEXT, `disqualify_reason` TEXT, `needs_human_review` INTEGER, `scored_at` TEXT NOT NULL.

### 5.7 `lead_search_evidence`

id PK, `session_id` INTEGER FK, `lead_id` INTEGER NOT NULL FK, `source` TEXT NOT NULL, `query` TEXT, `results` TEXT, `fetched_at` TEXT NOT NULL.

### 5.8 `export_history`

id PK, `session_id` INTEGER FK, `lead_id` INTEGER NOT NULL FK, `domain_normalized` TEXT NOT NULL, `exported_at` TEXT NOT NULL.

### 5.9 Indexes

`idx_sessions_created_at`, `idx_leads_session`, `idx_leads_email`, `idx_leads_domain`, `idx_leads_status`, `idx_content_session`, `idx_scores_session`, `idx_technical_signals_lead`, `idx_export_history_domain` — `db.py:621-629`; `idx_users_email` — `db.py:727-731`.

### 5.10 Idempotent additions

`init_db` adds missing columns via `_add_column` (idempotent — ignores "duplicate column" / "already exists", `db.py:64-67, 767-779`): `cancelled`, `scoring_criteria*`, `last_batch_ids`, `owner_id` (sessions); `review_*` (leads); `last_error`, `scrape_seconds`, `score_seconds` (leads); `ai_style_*` columns (signals) — `db.py:712-764`.

---

## 6. Authentication & roles

### 6.1 Rules

- **Signed session**: `session["user_id"]` + `session["role"]` stored at login — the role is NOT re-read from the DB on every request (comment `app.py:202-206`).
- **`is_active` re-read from the DB on EVERY request** (`app.py:208-211, 224-232`) — a blocked user is disconnected immediately; `session.clear()` + flash if blocked.
- **`_require_login`** (`@app.before_request`, `app.py:216-233`): excludes `PUBLIC_ENDPOINTS = {"login", "signup", "static"}` (`app.py:213`); login redirect with `next=request.path`.
- **`admin_required`** (decorator, `app.py:236-244`): `session.get("role") != "admin"` → flash + redirect `history`.
- **`_assert_session_access`** (`app.py:247-253`): admin → any access; otherwise `owner_id == user_id`; legacy sessions (owner_id NULL) reserved for the admin.
- **First account**: `create_user` → `role = "admin"` if `count_users == 0`, otherwise `"user"` — `db.py:442-456`.

### 6.2 Roles

| Role | Rights |
|---|---|
| `user` | Own sessions, dashboard, exports, review of own leads |
| `admin` | Everything + `/admin/users` (list, role change, block/unblock, delete, user history) |

Admin guardrails: impossible to demote/block/delete the **last active admin** (`count_active_admins <= 1`, `app.py:1266, 1282`); self-delete forbidden (`app.py:1304`).

### 6.3 Per-user numbering

`list_analysis_sessions` computes `user_rank = ROW_NUMBER() OVER (PARTITION BY s.owner_id ORDER BY s.id)` (`db.py:405-430`): each user sees their sessions numbered 1, 2, 3… in their own history; the admin sees the combined global numbering. Display conditioned on `show_rank` (`history.html:88`, `admin_user_history`).


---

## 7. Flask routes (32 routes)

### 7.1 Auth / Jinja filters

- `app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "lead-qualification-engine")` — `app.py:37`.
- Jinja filters: `map_offer`, `map_segment`, `map_status_label`, `badge_class`, `format_datetime`, `confidence_class` (thresholds aligned with `CONFIDENCE_THRESHOLD` and `INVALID_VERDICT_CONFIDENCE_CAP`) — `app.py:41-95`.
- In-memory progress: `_pipeline_progress: dict[int, dict]` + `_pipeline_lock` — `app.py:97-98`.

### 7.2 Routes table

| # | Method | Path | Function | Role | Parameters | Template |
|---|---|---|---|---|---|---|
| 1 | GET | `/` | `home` (app.py:409-421) | Home: stats + recent sessions | — | home.html |
| 2 | GET | `/history` | `history` (app.py:424-432) | Session history | — | history.html |
| 3 | GET | `/dashboard` | `dashboard` (app.py:435-504) | Dashboard: leads/scores tables, filters, lead detail | query: lead_id, session_id, segment (getlist), needs_review=1, hide_duplicates (default "1") | dashboard.html |
| 4 | POST | `/upload` | `upload_and_review` (app.py:507-540) | Step 1: CSV import → ingest + dedup → redirect review | form: csv_file, fuzzy_threshold (default 90) | redirect → import_review |
| 5 | GET | `/import/<session_id>` | `import_review` (app.py:543-573) | Step 2: lead review + scoring criteria selection | — (criteria_options hardcoded app.py:556-563) | import_review.html |
| 6 | POST | `/import/<session_id>/start` | `start_pipeline_from_review` (app.py:576-631) | Step 3: save criteria, re-include checked duplicates (SQL reset is_duplicate, app.py:597-601), mark unselected as SKIPPED (app.py:606-615), pipeline in thread | form: criteria (getlist), custom_criteria, throttle_seconds (default 12), concurrency (default `PIPELINE_CONCURRENCY`), lead_ids, dup_ids | redirect → progress_view |
| 7 | POST | `/analyze-pending/<session_id>` | `analyze_pending` (app.py:634-670) | Restart pending/SKIPPED leads | form: lead_ids, throttle_seconds (12), concurrency | redirect → progress_view |
| 8 | POST | `/session/<session_id>/delete` | `delete_session` (app.py:673-688) | Session deletion + data (explicit cascade) | query: next (redirect if internal path) | redirect history |
| 9 | GET | `/results/<session_id>` | `results_view` (app.py:744-795) | Step 4: results in 5 categories | — | results.html |
| 10 | POST | `/rescore/<session_id>` | `rescore_leads` (app.py:798-845) | Re-scoring without re-scraping; fallback = all LOW_CONFIDENCE leads or needs_human_review without disqualify api_error/no_content_scraped (app.py:811-823); DELETE of old lead_scores (app.py:832) | form: lead_ids | redirect → progress_view |
| 11 | POST | `/start-analysis` | `start_analysis` (app.py:849-890) | One-click full analysis: ingest + dedup + pipeline thread | form: csv_file, fuzzy_threshold (90), throttle_seconds (12), concurrency | redirect → progress_view |
| 12 | POST | `/ingest` | `ingest_only` (app.py:893-915) | Import only | form: csv_file | redirect → dashboard |
| 13 | POST | `/dedup` | `dedup_only` (app.py:918-932) | Dedup only | form: fuzzy_threshold (90); query: session_id | redirect → dashboard |
| 14 | POST | `/pipeline` | `pipeline_only` (app.py:935-957) | Pipeline only (scrape+score) | form: throttle_seconds (12), concurrency; query: session_id | redirect → progress_view |
| 15 | POST | `/lead/<lead_id>/review` | `review_lead` (app.py:960-986) | Human decision APPROVED/REJECTED + segment override | form: decision, segment; query: session_id (inferred if absent) | redirect → dashboard |
| 16 | GET | `/download/scraping.csv` | `download_scraping_csv` (app.py:989-997) | Scraping CSV | query: session_id | file `scraping_results_<ts>.csv` |
| 17 | GET | `/download/scores.csv` | `download_scores_csv` (app.py:1000-1014) | Scores CSV + cross-batch dedup (`run_export_dedup`) + `record_export` | query: session_id | file `scores_results_<ts>.csv` |
| 18 | GET | `/download/search.csv` | `download_search_csv` (app.py:1017-1026) | SGAI web search CSV | query: session_id | file `search_results_<ts>.csv` |
| 19 | GET | `/export/<session_id>/<format>` | `export_results` (app.py:1029-1075) | Complete CSV or PDF export (printable HTML) | format ∈ {csv, pdf} | csv → `complete_results_<ts>.csv`; pdf → results_print.html |
| 20 | GET | `/batch-results/<session_id>` | `batch_results_view` (app.py:1078-1104) | Results of the last batch (`last_batch_ids`) | — | batch_results.html |
| 21 | GET | `/web-search/<session_id>` | `web_search_view` (app.py:1107-1122) | Dedicated web search evidence page | — | web_search.html |
| 22 | GET | `/sessions/<session_id>` | `session_redirect` (app.py:1125-1130) | Redirect to results | — | — |
| 23 | GET | `/progress/<session_id>` | `progress_view` (app.py:1133-1139) | Real-time progress page | — | progress.html |
| 24 | GET | `/progress/<session_id>/stream` | `progress_stream` (app.py:1142-1174) | SSE: `data: {...}\n\n`, 'waiting' cycle every 1 s, 'running' every 0.5 s, break on completed/failed/cancelled; no-cache headers + X-Accel-Buffering no | — | text/event-stream |
| 25 | GET/POST | `/signup` | `signup` (app.py:1181-1204) | Sign-up; email + password ≥ 6 validations | form: email, password | signup.html |
| 26 | GET/POST | `/login` | `login` (app.py:1207-1232) | Login; `check_password_hash`; `update_last_login`; `is_active` checked | form: email, password; query: next | login.html |
| 27 | POST | `/logout` | `logout` (app.py:1235-1239) | `session.clear()` | — | redirect login |
| 28 | GET | `/admin/users` | `admin_users` (app.py:1246-1251) — @admin_required | User list | — | admin_users.html |
| 29 | POST | `/admin/users/<user_id>/role` | `admin_user_role` (app.py:1254-1271) — @admin_required | Role change; last-active-admin guardrail | form: role ∈ {admin, user} | redirect admin_users |
| 30 | POST | `/admin/users/<user_id>/toggle-active` | `admin_user_toggle_active` (app.py:1274-1290) — @admin_required | Block/unblock; last-admin guardrail | — | redirect admin_users |
| 31 | POST | `/admin/users/<user_id>/delete` | `admin_user_delete` (app.py:1293-1309) — @admin_required | Deletion; self-delete forbidden | — | redirect admin_users |
| 32 | GET | `/admin/users/<user_id>/history` | `admin_user_history` (app.py:1312-1321) — @admin_required | Sessions history of a user | — | admin_user_history.html |

### 7.3 Background threads

- `_background_pipeline` (app.py:146-174) and `_background_rescore_pipeline` (app.py:116-143): store the in-memory progress (`_pipeline_progress[session_id]`) and update the session status (completed/failed).
- Helpers: `_run_ingest` (tempfile suffix .csv, `batch_id = f"batch_{uuid.uuid4().hex[:8]}"` — app.py:392-406), `_summary_context`, `_session_summary`, `_load_dashboard_data`, `_csv_response` (app.py:298-389).

---

## 8. CSV ingestion

- `insert_leads_from_csv(conn, csv_path, batch_id, session_id=None)` — `db.py:801-868`: reads `utf-8-sig` via `csv.DictReader`; skips rows without a website (`skipped_no_website`); returns `{"inserted", "skipped_no_website"}`.
- **Expected columns**: first_name, last_name, title, company_name, email, website_url — with tolerated aliases (uppercase/spaces):

| Field | Accepted aliases |
|---|---|
| first_name | first_name, first name, firstname |
| last_name | last_name, last name, lastname |
| title | title, job title, person title |
| company_name | company_name, company, company name, organization |
| email | email, email address, work email |
| website_url | website_url, website, company website, website url |

(`COLUMN_ALIASES`, `db.py:70-77`; first non-empty alias column selected — `_pick_column`, `db.py:793-798`.)

- **`domain_mismatch`**: 1 if the email does not belong to a free provider (`FREE_EMAIL_PROVIDERS`: gmail, yahoo, outlook, hotmail, icloud, proton.me, protonmail, aol, gmx, live, yandex, mail, zoho — `db.py:79-83`) AND the email domain ≠ site domain; reason stored in `domain_mismatch_reason` (e.g. personal email on a company domain). A lead with `domain_mismatch=1` is **forcibly** given `needs_human_review=True` at scoring (`pipeline.py:248-256`).

---

## 9. Deduplication

Principle (`dedup.py:1-12`): **never deletes**, only sets the `is_duplicate` flag; a lead already flagged as duplicate is never compared (no duplicate chains); insertion order prevails — the first seen stays "the original" (`dedup.py:21-24`).

`run_dedup(conn, fuzzy_threshold=90, session_id=None)` — `dedup.py:18-80`:

| Level | Method | Condition | duplicate_reason |
|---|---|---|---|
| 1 | Exact email | `(email).strip().lower()` already seen | `exact_email` |
| 2 | Normalized domain | `domain_normalized` (strip/lower) already seen | `domain_match` |
| 3 | Fuzzy company name | `fuzz.token_sort_ratio(company, other_company) >= fuzzy_threshold` (default 90) | `fuzzy_company_name` |

Batch write: `executemany` + commit (`dedup.py:73-78`); returned stats: `{"exact_email", "domain", "fuzzy_company", "kept_original"}`.

**Cross-batch dedup**: `check_against_export_history(conn, exported_domains)` marks `duplicate_reason='already_exported_previous_batch'` (is_duplicate=1, duplicate_of_id=NULL) for any lead whose domain appears in `export_history` (`dedup.py:83-101`); `run_export_dedup` (dedup.py:104-110) is called automatically when the scores CSV is downloaded (`app.py:1000-1014`). "already exported" badge in the categorization (`app.py:707-717`).


---

## 10. Firecrawl scraping

### 10.1 Constants & pattern lists

- `KEYWORDS` (scraper.py:27-32): about → [about, team]; pricing → [pricing, plans, price]; careers → [careers, jobs]; product → [product, services, solutions, features].
- `COMMON_PATH_CANDIDATES` (scraper.py:39-44): standard paths (/about, /pricing…) tried as fallback.
- `MAX_CONTENT_CHARS_PER_PAGE = 32000` (scraper.py:46).
- `BROKEN_PAGE_MARKERS` (scraper.py:56-65): 8 literal markers ("client-side exception has occurred", "application error", "hydration failed", "unhandled runtime error", "this page could not be found", "404 not found", "404: this page could not be found", "500 internal server error").
- `BROKEN_PAGE_PATTERNS` (scraper.py:70-76): 5 regexes (Markdown 404, "404 … page not found", reverse order, "oops! … vanished", "page you're looking for … doesn't exist/not found/vanished").
- `MIN_VALID_CONTENT_CHARS = 50` (scraper.py:78).
- `GENERATOR_FINGERPRINTS` (scraper.py:86-93): lovable (lovable.dev, lovable-tagger, gpteng.co), v0 (v0.dev, vusercontent.net), bolt (bolt.new, stackblitz), replit (replit.com, replit.dev), cursor (built with cursor, cursor.sh).
- `TREND_FONTS` (scraper.py:95-97): Space Grotesk, Instrument Serif, Geist, Syne, Fraunces.
- `VISUAL_PATTERNS` (scraper.py:99-109): 9 patterns — purple_accent, gradient, glassmorphism, colored_glow, numbered_steps, stat_banner, headline_badge, faq_accordion, shadcn_ui.
- `VIBE_LANGUAGE_MARKERS` (scraper.py:111-114): "built with cursor", "built with v0", "made with lovable", "built with bolt", "vibe coded", "vibe-coded", "no-code".
- `AI_STYLE_PHRASES` (scraper.py:122-135): 34 phrases ("seamless integration", "unlock the power of", "game-changer", …).
- `AI_AUTHORSHIP_DISCLOSURES` (scraper.py:139-142): "written with ai", "generated with ai", "powered by gpt", "powered by chatgpt", "ai-generated content", "content generated by ai", "drafted by ai".
- Careers/pricing signals: `ENGINEERING_ROLE_KEYWORDS`, `OTHER_ROLE_KEYWORDS`, `SELF_SERVE_CTA_MARKERS`, `SALES_LED_CTA_MARKERS`, `VISIBLE_PRICE_PATTERN` (scraper.py:153-176).

### 10.2 Firecrawl key pool

- `_get_client_pool()` (scraper.py:251-269): one `Firecrawl(api_key=key, timeout=120)` instance per key among `FIRECRAWL_API_KEY`, `_2`, `_3`, `_4`, `_5` (scraper.py:262); RuntimeError if no key.
- `_is_quota_error`: "insufficient credits", "402", "429 quota", "billing" patterns (scraper.py:272-279); `_parse_retry_after`: regex `retry after\s+([\d.]+)\s*s` +1.0 s margin (scraper.py:282-291).
- `_firecrawl_scrape` (scraper.py:294-341): round-robin over live keys, `max_rounds = 2`; key in quota → marked dead (`_client_pool_dead`); rate-limited key → next key; all rate-limited → wait for the retry-after (15 s default) then one more pass; non-quota/non-rate-limit error → raise immediately.
- **Parallelism**: `_scrape_pages_in_parallel` (scraper.py:344-387) — `n_workers = max(1, min(len(clients), len(urls_by_category)))`: **1 thread max per key** (no internal throttling with multiple keys); sequential throttled mode with a single key.
- Firecrawl calls: `client.scrape(url, formats=["markdown", "links"], only_main_content=True, timeout=10000)` (scraper.py:361, 371); homepage: `formats=["markdown", "rawHtml", "links"], timeout=10000` (scraper.py:505).

### 10.3 Anti-fake-page filters

| Function | Role |
|---|---|
| `_normalize_domain` (scraper.py:390-401) | netloc without www, lowercase |
| `_is_same_domain` (scraper.py:404-413) | prevents an external link (g2.com, blog, LinkedIn) containing a keyword from being chosen as a key page |
| `_is_real_subpage` (scraper.py:416-436) | rejects `#services` anchors and any link pointing to the same page as the homepage (one-page sites); SPAs with identical content are filtered a posteriori by hash |
| `_url_exists` (scraper.py:439-458) | HEAD first; if 405/501 (Vercel/Netlify) → GET stream; status < 400 = exists — avoids spending a Firecrawl credit on a 404 |
| `_looks_broken` (scraper.py:461-480) | text < 50 chars → True; otherwise literal markers then regex (case-insensitive) |
| `_content_fingerprint` (scraper.py:483-499) | sha256 of the normalized text (images/URLs/base64 removed) — detects identical SPA pages |

### 10.4 `_find_key_pages(homepage_url)` (scraper.py:502-560)

1. Scrapes the homepage (`formats=["markdown", "rawHtml", "links"]`).
2. Filters `all_links` by `_is_real_subpage` then restricts to the same domain.
3. For each `KEYWORDS` category: first same-domain link containing a keyword.
4. Fallback: standard paths (`COMMON_PATH_CANDIDATES`) checked via `_url_exists`.
5. "product" catch-all: first unassigned link.
6. Returns `(found_pages, result, all_links)` — **`all_links` NOT domain-filtered** (needed for the external GitHub link).

### 10.5 `scrape_website(homepage_url, throttle_seconds=1.0)` (scraper.py:688-824) — step-by-step flow

1. `_find_key_pages`; any exception → **FETCH_FAILED**, rows=[], error (`scraper.py:703-713`).
2. `homepage_markdown = homepage_result.markdown or ""` (scraper.py:715).
3. `_looks_broken(homepage_markdown)` → **FETCH_FAILED**, error `"homepage_render_error_or_empty_content"` (scraper.py:717-725).
4. `rows = [("homepage", homepage_url, homepage_markdown[:32000])]`; `seen_fingerprints` initialized with the homepage hash (scraper.py:727-728).
5. Parallel scrape of the other pages (`_scrape_pages_in_parallel`, scraper.py:736).
6. **Correction 1 — GitHub links outside the homepage** (scraper.py:738-789): the links of already-scraped pages (`r.links`) are filtered by `_is_real_subpage` and merged (deduplicated) into `all_links` — the same GitHub repo in several page footers is only counted once.
7. For each key page: failure → `failures += 1`; `_looks_broken` → `failures += 1`; fingerprint already seen → `duplicates += 1` (SPA shell); otherwise: **careers** replaced by the formatted `extract_careers_signal` signal, **pricing** by the formatted `extract_pricing_signal` (`_format_signal_as_text`, scraper.py:237-243), other pages raw text (scraper.py:772-779).
8. `extract_technical_signals(raw_html, all_links, homepage_text)` (scraper.py:791-795).
9. `github_check = check_github_repo_pattern(...)` if `github_repo_url` found (scraper.py:797-799).
10. `unusable = failures + duplicates` (scraper.py:804).
11. **Status logic** (scraper.py:806-816):
    - `unusable > 0` → **FETCH_PARTIAL** (scraper.py:813-814) — even if ALL sub-pages are unusable, as soon as the homepage exists it is NOT FETCH_FAILED.
    - otherwise → **PARSED** (scraper.py:815-816).
    - **FETCH_FAILED is reserved for the truly dead site** (homepage unreachable or broken → rows == [], handled upstream) (scraper.py:806-812).
12. Returns: `{"status", "rows", "technical_signals", "github_check", "error"}` (scraper.py:818-824).

> Semantics (fix `cecc9b3`): a human looking at the dashboard must be able to distinguish "totally dead site" (FETCH_FAILED, no pages) from "site up, poor sub-pages" (FETCH_PARTIAL, homepage recovered).

### 10.6 `check_github_repo_pattern(repo_url)` (scraper.py:648-685)

- Unauthenticated public GitHub API: `GET https://api.github.com/repos/{owner}/{repo}/commits?per_page=100` timeout=10 (scraper.py:666-670).
- Returns: `{"repo_url", "checked", "evidence": {"total_commits_seen", "first_commit_message", "single_commit_repo"}, "error"}` — `single_commit_repo = len(commits) <= 1` (scraper.py:658, 677-681).

---

## 11. Web escalation (ScrapeGraphAI)

- `_SGAI_BASE_URL = "https://v2-api.scrapegraphai.com/api"` (scraper.py:835).
- `SEARCH_QUERY_TEMPLATES` (scraper.py:841-851):

| Source | Query |
|---|---|
| linkedin | `"{company}" site:linkedin.com/in OR site:linkedin.com/company` |
| product_hunt | (dedicated template) |
| twitter | `"{company}" ... (vibe coded OR built with AI OR built in a weekend)` |
| github | (dedicated template) |
| interviews | `"{founder}" OR "{company}" interview (vibe coding OR built with AI OR built with Cursor OR built with v0)` |
| person_linkedin | `"{founder}" site:linkedin.com/in` |
| person_github | `"{founder}" site:github.com` |

- `_get_sgai_keys()` (scraper.py:854-865): `SGAI_API_KEY` to `_5`; RuntimeError if none (scraper.py:880).
- `_sgai_request` (scraper.py:868-914): POST `{base}/{path}` header `SGAI-APIKEY`; quota → key marked dead; all exhausted → raise.
- `_sgai_search_one(source, query, limit_per_query)` (scraper.py:917-935): POST /search `{"query": query, "numResults": limit_per_query}` timeout=35; extracts url/title/content; `{"error": ...}` on exception.
- `_sgai_linkedin_full_scrape(results, prefer_profile=False)` (scraper.py:938-990): full scrape of the best LinkedIn page (`/company/` by default, `/in/` if `prefer_profile`) via POST /scrape `{"url":..., "formats": [{"type":"markdown"},{"type":"json","prompt":...}]}` timeout=45; best-effort (failure keeps the snippets).
- `search_additional_evidence(company_name, founder_name=None, limit_per_query=3, throttle_seconds=1.0)` (scraper.py:993-1055): without keys → `{"_error": "SGAI_API_KEY not configured in .env"}`; skips `{founder}` templates without a founder name; 1 thread per key; LinkedIn full scrape for "linkedin" and "person_linkedin".

---

## 12. Deterministic technical signals

`extract_technical_signals(raw_html, all_links, homepage_text="")` (scraper.py:701-823):

| Signal | Extraction |
|---|---|
| `generator_fingerprint` | null if none — tested on raw_html (scraper.py:739-741) |
| `generator_meta_tag` | regex `<meta ... name="generator" content="...">` (scraper.py:730-735) |
| `vibe_language_matches` | markers present in raw_html.lower() (scraper.py:746-750) |
| `trend_fonts_found` | font names present (scraper.py:751-753) |
| `visual_patterns_triggered` | names of the 9 matched patterns (scraper.py:754-756) |
| `ai_style_phrases_found` + `ai_style_phrase_density` | counted on the visible text; ≥4 → "high", ≥2 → "medium", ==1 → "low", otherwise "none". Factored out into `extract_text_style_signals(text)` (scraper.py:673-695) — reused by the scraping CSV to recompute **per page** (Correction: phrases never repeated from one page to another) |
| `ai_authorship_disclosures_found` | (included in `extract_text_style_signals`, scraper.py:673-695) |
| `github_repo_url` | first link containing "github.com" excluding /issues and /pull — **deliberately not domain-filtered** (scraper.py:772-779) — collected on homepage AND sub-pages (Correction 1) |
| `hiring_technical` | boolean from the deterministic careers signal (`extract_careers_signal`, scraper.py:285-307), added by `scrape_website` to the `technical_signals` (scraper.py:1023) — **read by pipeline.py as a condition of the web escalation trigger** (pipeline.py:255), must not break |

These signals are persisted in `lead_technical_signals` and passed to the scorer (reliability hierarchy in [§13](#13-groq-scoring)).


---

## 13. Groq scoring

### 13.1 Constants

- `MODEL = "llama-3.3-70b-versatile"` (scorer.py:44); `MAX_SITE_CONTENT_CHARS = 12000`; `MAX_WEB_EVIDENCE_CHARS = 12000` (equal site/web budgets); `MAX_OUTPUT_TOKENS = 2048`; retry: `RETRY_MAX_CONTENT_CHARS = 6000`, `RETRY_MAX_OUTPUT_TOKENS = 1024` (scorer.py:45-51).
- `INVALID_VERDICT_CONFIDENCE_CAP = 0.3` (scorer.py:55) — confidence cap when the verdict is force-corrected.
- `VALID_OFFERS = {"ai_audit", "general_audit", "pipeline", "none"}` (scorer.py:303); `VALID_STAGES = {"pre-launch", "early", "scaling", "established"}` (scorer.py:304).

### 13.2 SYSTEM_PROMPT structure (scorer.py:57-183)

1. **Role**: "senior analyst who evaluates B2B leads for a technical development agency (RuyaTech)".
2. **THE TWO OFFERS WE SELL**: Technical audit (ai_audit / general_audit) + AI lead-gen pipeline (pipeline).
3. **OUR PRIMARY TARGET**: non-technical founders using AI (vibe coding, Cursor, Bolt, Lovable, Replit).
4. **SEGMENTS**: 6 segments + recommended offer; `unclear` → `needs_human_review` necessarily true, offer none unless a partial signal; warning not to confuse unclear/wrong_field/too_big.
5. **RELIABILITY HIERARCHY OF DETERMINISTIC SIGNALS** (scorer.py:94-125):
   - **STRONG** (near-proof): generator_fingerprint non-null, ai_authorship_disclosures_found non-empty, github_check.single_commit_repo=true + generator_fingerprint.
   - **MEDIUM**: vibe_language_matches non-empty, ai_style_phrase_density "high".
   - **WEAK** (never sufficient alone): visual_patterns_triggered.
   - Isolated uncorroborated fingerprint → lower the confidence.
   - **Site vs Web search: EQUAL WEIGHT**.
   - `person_*` sources = PRIORITY signal (the founder's own profile).
6. **CURSOR — SPECIAL RULE** (scorer.py:127-137): a bare Cursor mention is NEVER sufficient alone for `ai_solo_founder`; must be corroborated by `github_check.single_commit_repo = true` OR a person_linkedin/person_github profile without a technical background; otherwise → unclear or technical_founder.
7. **RULES** (scorer.py:139-168): (1) every cited signal must have an exact citation in evidence_quotes; (2) situational hooks, never biographical; (3) confidence < 0.7 → needs_human_review:true; (4) full 0.0-1.0 spectrum; (5) use ONLY the provided text; (6) fictional examples/demos are not facts; (7) distinguish "product with AI features" vs "team built with AI tools"; (8) the contact's title is a direct signal (CTO/Lead Engineer → technical_founder; Founder/CEO alone → ai_solo_founder if corroborated); (9) ordered questions a→f.
8. **Verdict JSON schema** (scorer.py:170-183):

```json
{
  "segment": "ai_solo_founder | technical_founder | small_agency_scaling | too_big | wrong_field | unclear",
  "confidence": 0.0,
  "company_stage": "pre-launch | early | scaling | established",
  "built_with_ai_signals": [],
  "technical_signals": [],
  "pain_signals": [],
  "evidence_quotes": [],
  "recommended_offer": "ai_audit | general_audit | pipeline | none",
  "personalization_hooks": [],
  "disqualify_reason": null,
  "needs_human_review": false
}
```

### 13.3 Groq calls

- `_get_client()` (scorer.py:188-192): OpenAI SDK pointed at `https://api.groq.com/openai/v1`.
- `_call_llm(user_content, max_output_tokens=2048)` (scorer.py:378-391): `model=MODEL, temperature=0.2, max_tokens, response_format={"type": "json_object"}`.
- Detection: `_is_rate_limit_error` (413/429 or "rate_limit_exceeded"/"rate limit"); `_is_json_parse_error` (400 or JSONDecodeError/KeyError/TypeError/ValueError) — scorer.py:361-375.

### 13.4 Post-LLM guards (in order)

| Guard | Behavior |
|---|---|
| `_apply_confidence_guard` (scorer.py:474-479) | `confidence < 0.7` → `needs_human_review=True` |
| `_validate_verdict` (scorer.py:386-421) | segment out of list → forced "unclear" + note `invalid_segment_fixed_to_unclear`; offer out of list → "none"; stage out of list → None; if corrected → `confidence = min(conf, 0.3)` + needs_human_review |
| `_verify_evidence_grounding` (scorer.py:578-616) | every evidence_quote must appear word for word in the source text (space/case normalization); ungrounded ones are removed + note `ungrounded_evidence_quotes_removed: N…` + needs_human_review |
| `_third_party_spans` (scorer.py:496-565) | spans of `[ATTRIBUTED QUOTE]`/`[THIRD-PARTY CONTENT SECTION]` blocks not attributed to the lead; excluded from citations (LOW, removed + note + needs_human_review). **Attribution = signature lines AFTER the blockquote** (name/title/company): the founder's name quoted **inside** a client quote ("Oussama launched it in two weeks") does NOT suffice to keep it — it stays excluded; without an attribution line → always excluded. **Heading sections** ("Testimonials", "Our work"…): attribution evidence limited to the **heading + first content line** — a name/company appearing LATER (quote body, closing boilerplate "Founded by Oussama") does not rescue the section (Correction) |
| `_apply_site_missing_guard` (scorer.py:685-710) | if `site_content_missing` → needs_human_review=True **unconditionally** + note `site_content_missing: no official site content available…` added if absent |

- `SITE_MISSING_INSTRUCTION` (scorer.py:712-722): instruction added to the prompt when the official site is unavailable (do not invent content, do not treat the absence as a signal, needs_human_review=true if the verdict relies only on the web).
- `_retry_after_failure` (scorer.py:668-683): retry with shortened content (6000 chars) / 1024 tokens; failure → `_empty_verdict(f"json_parse_failed: … | retry_error: …")`.
- `_empty_verdict` (scorer.py:423-438): segment="unclear", confidence=0.0, offer="none", needs_human_review=True.

### 13.5 `score_content(...)` (scorer.py:725-840)

Signature: `rows, deterministic_signals=None, lead_metadata=None, web_search_evidence=None, scoring_criteria=None, scoring_criteria_custom="", site_content_missing=False`.

- `rows_to_text(rows, max_chars=12000)` — accepts tuples AND dicts (rescore bug fixed, scorer.py:286-289); `_format_web_search_evidence` — `person_*` sources ordered first (scorer.py:254-257); `_format_lead_metadata` (Name/Title/Company/Email/Website); `_strip_images` removes images/media/markdown images (scorer.py:195-205).
- No site NOR web content → `_empty_verdict("no_content_scraped")` (scorer.py:528-529).
- `build_user_content` (scorer.py:531-574): metadata → site → web evidence → SITE_MISSING_INSTRUCTION (if flag) → user criteria (dict by key: ai_solo_founder, technical_founder, solo_or_small, agency_or_studio, no_ai, wrong_field) → indented deterministic_signals JSON + hierarchy reminder.
- Chain: `_call_llm` → confidence_guard → validate → grounding → site_missing_guard (scorer.py:576-599); JSONDecodeError/parse → retry; rate-limit → shortened retry, failure → `_empty_verdict(f"api_error_after_retry: …")`; other exceptions → re-raise.

---

## 14. Orchestrator pipeline

### 14.1 Constants

- `DEFAULT_THROTTLE_SECONDS = 15` — "Firecrawl free tier ~10 req/min" (pipeline.py:19).
- `DEFAULT_CONCURRENCY = int(os.getenv("PIPELINE_CONCURRENCY", "3"))` (pipeline.py:20).

### 14.2 `_process_lead(...)` (pipeline.py:130-292) — one lead, step by step

1. **Dedicated DB connection** per lead (pipeline.py:131); returns progress events (`_base`, pipeline.py:136-145).
2. **Scraping**: `scraper.scrape_website(website, throttle_seconds=1.0)` (pipeline.py:153); exception → `FETCH_FAILED` + scrape_seconds (pipeline.py:154-159); otherwise `update_lead_progress` with `scrape_result["status"]` (pipeline.py:162), `save_lead_content` if rows (pipeline.py:163-164), `save_lead_technical_signals` (pipeline.py:169-175).
3. **Pass 1 (site only)**: `deterministic_signals` = technical_signals + github_check (pipeline.py:182-185); **`site_content_missing = not any((content or "").strip() for _, _, content in scrape_result["rows"])`** — based on the ACTUAL rows content, not the status (fix `cecc9b3`, pipeline.py:189-199); `_score(web_evidence={})` (pipeline.py:201-210); pass-1 exception → SCORE_FAILED (pipeline.py:214-219).
4. **Conditional web escalation (FR-3, additive rule)**: if `needs_human_review` OR `confidence < 0.7` OR (`segment == "small_agency_scaling"` AND `technical_signals.hiring_technical == True` AND `confidence >= 0.7`) → `_fetch_web_search_evidence` (call `search_additional_evidence(company_name, founder_name, limit_per_query=2)`, persisted via `save_search_evidence`) then a second `_score(web_evidence)` (pipeline.py:239-272); pass-2 failure → keep the pass-1 verdict with note `web_escalation_second_pass_failed: …` (pipeline.py:263-271). The 3rd block (confident high-value agency) ADDS to the existing net, it does not replace it; `hiring_technical` comes from the deterministic careers signal (scraper.py:797-801), never from the LLM verdict.
5. **Save**: applies `domain_mismatch` (→ forced needs_human_review + note, pipeline.py:248-256); `save_lead_score`; `new_status = "LOW_CONFIDENCE" if needs_human_review else "SCORED"` (pipeline.py:258-259); `update_lead_progress(status, error=scrape_err)` — a status write always overwrites last_error (None clears it, db.py:968-999); exception → SCORE_FAILED (pipeline.py:262-266).
6. **Emitted events**: `scraping`, `scraping_done`, `web_search`, `done` (pipeline.py:166, 195, 260, 300).
7. **Error isolation**: global fatal try/except + `finally: conn.close()` (pipeline.py:270-274) — a crashed lead never fails the others.

### 14.3 `run_pipeline(...)` (pipeline.py:277-321)

- Loads criteria + `get_leads_to_process` (filters `is_duplicate = 0` + `status IN (NOT_YET_SCORED_STATUSES)` — db.py:906-919); `total == 0` → return.
- **Sequential** if `concurrency <= 1 or total == 1`: `_sleep_check(throttle_seconds)` between each lead (pipeline.py:306-311).
- **Parallel** otherwise: `ThreadPoolExecutor(max_workers=min(concurrency, total))`, each future = `_process_lead` with its own connection; yiels in completion order (pipeline.py:313-321).

### 14.4 `run_rescore_pipeline(...)` (pipeline.py:324-416)

- **No re-scraping and no web search** (pipeline.py:325-327); loads `get_leads_by_status(lead_status="RESCORE_PENDING")`.
- No scraped content → `RESCORE_FAILED` + error `"no_scraped_content"` (pipeline.py:354-359).
- Reloads the deterministic signals, recomputes `site_content_missing`, **reloads the persisted web evidence** (rescore bug fixed: web evidence used to disappear — `_load_persisted_web_evidence`, pipeline.py:94-109, 361-377).
- Same guards (domain_mismatch, LOW_CONFIDENCE/SCORED status); exception → SCORE_FAILED (pipeline.py:406-412); `_sleep_check(throttle_seconds)` between leads.

---

## 15. Real-time progress (SSE)

- Endpoints (progress.html:164-166): `PROGRESS_URL = url_for('progress_stream', ...)` (EventSource); `RESULTS_URL` → auto redirect 5 s after completion (progress.html:342-344).
- SSE stream (`progress_stream`, app.py:1142-1174): `data: {...}\n\n`; 'waiting' cycle every 1 s, 'running' every 0.5 s; break on completed/failed/cancelled; headers `Cache-Control: no-cache`, `Connection: keep-alive`, `X-Accel-Buffering: no`.
- JS (progress.html): `connectSSE()`; `translateStep` (scraping / scraping_done / scoring / done / waiting); `translateStatus` (FETCH_FAILED, FETCH_PARTIAL, PARSED, SCORE_FAILED, LOW_CONFIDENCE, SCORED); Import/Scraping/Scoring segments; SSE error → reconnect after 3 s; local 1 s timer.
- Progress stored in-memory on the app side (`_pipeline_progress[session_id]`: index/total, current lead, step, started_at, completed_ts, errors).

---

## 16. Exports

`export.py` — 3 historical formats + web search; "reporting functions, no lifecycle operations" (export.py:1-15). `_flatten`: None → ""; JSON string → parse+join; list → join " | "; dict → compact JSON (export.py:25-46).

### 16.1 Scraping CSV — `SCRAPING_FIELDS`, 20 columns (export.py:53-61)

`lead_id, company_name, website_url, status, error, source, url, content_chars, content, generator_fingerprint, generator_meta_tag, trend_fonts_found, visual_patterns_triggered, vibe_language_matches, github_repo_url, github_check, ai_style_phrases_found, ai_style_phrase_density, ai_authorship_disclosures_found`

- `_iter_scraping_rows` (export.py:64-128): one row per page; leads without content → one empty row (empty source/url, content_chars 0) to **never lose any lead**; `utf-8-sig` encoding.
- **Per-page signals (Correction)**: the 3 writing-style signals (`ai_style_phrases_found`, `ai_style_phrase_density`, `ai_authorship_disclosures_found`) are **recomputed on the text of each page** (via `scraper.extract_text_style_signals`), never inherited from the homepage; the HTML/DOM signals (`generator_fingerprint`, `generator_meta_tag`, `trend_fonts_found`, `visual_patterns_triggered`, `vibe_language_matches`) are only reported **on the homepage row** (source != "homepage" → empty) — they are only computed on the homepage raw HTML.

### 16.2 Scoring CSV — `SCORE_FIELDS`, 23 columns (export.py:144-151)

`lead_id, first_name, last_name, title, company_name, email, website_url, status, error, is_duplicate, duplicate_reason, segment, confidence, needs_human_review, company_stage, recommended_offer, disqualify_reason, built_with_ai_signals, technical_signals, pain_signals, evidence_quotes, personalization_hooks, scored_at`

- Via `get_leads_with_scores` (**latest verdict**: `s.id = (SELECT MAX(id) FROM lead_scores WHERE lead_id = l.id)` — db.py:1289-1311).

### 16.3 Readable CSV (human review) — `READABLE_FIELDS`, 15 columns (export.py:228-235)

`lead_id, company_name, website_url, status, segment, confidence, needs_human_review, recommended_offer, disqualify_reason, signals_summary, github_check_summary, homepage_preview, about_preview, product_preview, pricing_preview, careers_preview, evidence_quotes, personalization_hooks, search_evidence`

- `DEFAULT_PREVIEW_CHARS = 400`; `_preview`: newlines flattened, truncation with " …", "(page not found / not scraped)" if empty; `_format_signals_summary` (readable sentence: generator, fonts, x/9 patterns, vibe language, phrases + density, disclosures, GitHub repo); `_format_github_check_summary` ("N commits seen (API page)", single-commit, first message 60 chars); `search_evidence` = `{src}: {titres} ||| …`.
- ⚠️ TODO code (comment export.py:323-326): `pricing_preview`/`careers_preview` remain raw text — dedicated extractors ("self-serve vs sales-only" and "N engineering jobs") to code.

### 16.4 Web search CSV — `SEARCH_FIELDS` (export.py:412-415)

`lead_id, company_name, website_url, source, query, result_url, result_title, result_snippet` — one row per result; snippet truncated to 500 chars; error → result_title "ERROR".

### 16.5 CLI

`main()` (export.py:482-500): `--scraping-out` (default scraping_results.csv), `--scores-out` (scores_results.csv), `--search-out` (search_results.csv); `init_db` (no-op) then the 3 exports.

---

## 17. Segments, statuses & state machine

### 17.1 Constants (constants.py — single source of truth, never redefined locally)

```python
VALID_SEGMENTS = {"ai_solo_founder", "technical_founder", "small_agency_scaling", "too_big", "wrong_field", "unclear"}
TARGET_SEGMENTS = {"ai_solo_founder", "technical_founder", "small_agency_scaling"}
OUT_OF_TARGET_SEGMENTS = {"too_big", "wrong_field"}
NOT_YET_SCORED_STATUSES = ("NEW", "PARSED", "FETCH_PARTIAL", "FETCH_FAILED", "SCORE_FAILED", "RESCORE_PENDING", "RESCORE_FAILED")
CONFIDENCE_THRESHOLD = 0.7  # FR-3
```

### 17.2 Segments and offers

| Segment | Description | Recommended offer |
|---|---|---|
| `ai_solo_founder` | Non-technical founder building with AI (PRIMARY TARGET) | `ai_audit` |
| `technical_founder` | Technical team, AI as a dev tool | `general_audit` |
| `small_agency_scaling` | Agency / studio in a scaling phase | `pipeline` |
| `too_big` | Established company, far from the target persona | `none` |
| `wrong_field` | Unrelated sector | `none` |
| `unclear` | Insufficient evidence | `none` (unless a partial signal) |

### 17.3 Lead status state machine

```
NEW
  └→ PARSED | FETCH_PARTIAL | FETCH_FAILED      (scraping)
        └→ SCORED | LOW_CONFIDENCE | SCORE_FAILED   (scoring)
              └→ APPROVED | REJECTED                 (human review)
```

| Status | Meaning | Set by |
|---|---|---|
| NEW | Lead imported, not yet processed | ingestion |
| PARSED | Complete scraping (all pages usable) | scraper |
| FETCH_PARTIAL | Homepage OK, ≥1 sub-page unusable (broken, SPA, 404) | scraper |
| FETCH_FAILED | Dead site: homepage unreachable or broken (rows == []) | scraper |
| SCORED | Verdict confidence ≥ 0.7, no residual doubt | pipeline |
| LOW_CONFIDENCE | Verdict needs_human_review (confidence < 0.7, or a guard) | pipeline |
| SCORE_FAILED | LLM/pipeline scoring error | pipeline |
| RESCORE_PENDING / RESCORE_FAILED | Re-scoring without re-scraping | rescore |
| SKIPPED | Not selected at import review | app (start_pipeline) |
| APPROVED / REJECTED | Human decision | review_lead |

### 17.4 Results page categories (`_categorize_leads`, app.py:691-741)

| Category | Criterion |
|---|---|
| Pending | status ∈ NOT_YET_SCORED_STATUSES |
| To review | needs_human_review |
| Ready to approve | segment ∈ TARGET_SEGMENTS |
| Out of target | segment ∈ OUT_OF_TARGET_SEGMENTS |
| Not selected | status SKIPPED |

---

## 18. Sequence housekeeping

Objective: after row deletions, the next identifiers resume at `MAX(id)+1` (no numbering gaps).

- `_SEQUENCE_TRIGGER_TABLES` (db.py:633-637): the 8 tables — analysis_sessions, users, leads, lead_content, lead_technical_signals, lead_scores, lead_search_evidence, export_history.
- `_sequence_housekeeping_sql()` (db.py:640-700) returns 3 **standalone** statements (the `;` split of `executescript` would break dollar-quoted bodies):
  1. `CREATE OR REPLACE FUNCTION public.sync_seq_after_delete()` (db.py:661-680): reads `pg_get_serial_sequence(...)`, computes `COALESCE(MAX(id), 0) + 1`, `setval(seq_name, next_val, false)`.
  2. One `trg_seq_<table>` trigger **AFTER DELETE FOR EACH STATEMENT** per table, created idempotently via `IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = ...)`.
  3. One-off realignment: `setval(...)` for each table (db.py:689-698).
- `_ensure_sequence_housekeeping(conn)` (db.py:703-709) executes the 3 statements + commit; called at the end of `init_db`.

---

## 19. Templates & UI

| Template | Role |
|---|---|
| `home.html` | Home: 6 stats, upload CSV dropzone, recent sessions; Users link if admin (home.html:117-119) |
| `signup.html` | Sign-up — "The first account of the database automatically becomes an admin" (signup.html:54) |
| `login.html` | Login |
| `history.html` | Session history (user rank column if show_rank, history.html:88) |
| `dashboard.html` | Dashboard: session selector, stats, ingestion, quick actions, leads/scores tables, lead detail |
| `import_review.html` | Import review (Step 2/4): keepers table, duplicates table, criteria cards (wrong_field hidden, import_review.html:180-188), custom criterion, launch |
| `progress.html` | Real-time progress (SSE — see §15) |
| `results.html` | Results in 5 categories + CSV buttons |
| `results_print.html` | Printable / PDF version (4 categories, `@page landscape` — results_print.html:7) |
| `batch_results.html` | Results of the last analyzed batch |
| `web_search.html` | Web search evidence per lead (300-char snippets, web_search.html:119) |
| `admin_users.html` | User management (role, block/unblock, delete, history link) |
| `admin_user_history.html` | Sessions history of a user |
| `static/styles.css` | Single stylesheet (dark Bootstrap theme) |


---

## 20. Environment variables

| Variable | Usage | Required | Supported |
|---|---|---|---|
| `DATABASE_URL` | PostgreSQL (Neon) connection | ✅ (else RuntimeError at startup) | — |
| `FIRECRAWL_API_KEY` | Main Firecrawl key | ✅ (else flash warning) | — |
| `FIRECRAWL_API_KEY_2` … `_5` | Additional Firecrawl keys (pool, round-robin, 1 thread/key) | ❌ | — |
| `GROQ_API_KEY` | Groq key for scoring | ✅ (else flash warning) | — |
| `SGAI_API_KEY` | Main ScrapeGraphAI key (web escalation) | ❌ (else escalation disabled) | — |
| `SGAI_API_KEY_2` … `_5` | Additional SGAI keys | ❌ | — |
| `DB_POOL_MINCONN` | Pool min (default 1) | ❌ | db.py:60 |
| `DB_POOL_MAXCONN` | Pool max (default 8) | ❌ | db.py:61 |
| `PIPELINE_CONCURRENCY` | Pipeline concurrency (default 3) | ❌ | pipeline.py:20 |
| `FLASK_SECRET_KEY` | Flask secret key (default "lead-qualification-engine") | ❌ | app.py:37 |
| `PORT` | HTTP port (default 5000) | ❌ | app.py:1326 |

> ⚠️ No `.env.example` in the repo. The `_4`/`_5` variables and the other optional variables are not in the current `.env` but are read by the code.

---

## 21. Dependencies & files

### 21.1 requirements.txt (8 dependencies, no versions)

pandas, rapidfuzz, firecrawl-py, openai, python-dotenv, flask, requests, psycopg2-binary

### 21.2 Repo files (root)

| File | Size | Role |
|---|---|---|
| `app.py` | ~55 KB | Flask interface |
| `constants.py` | ~0.6 KB | Segments, statuses, threshold |
| `db.py` | ~46 KB | Schema + CRUD + pool |
| `dedup.py` | ~4 KB | 3 dedup levels |
| `export.py` | ~22 KB | CSV/CLI exports |
| `pipeline.py` | ~19 KB | Orchestrator |
| `scorer.py` | ~29 KB | Groq scoring |
| `scraper.py` | ~43 KB | Scraping + signals + SGAI |
| `README.md` | ~2.4 KB | Short doc (⚠️ unclosed code block bug) |
| `requirements.txt` | 90 B | Dependencies |
| `.env` | 571 B | Secrets (gitignored) |
| `.gitignore` | 86 B | .env, __pycache__/, *.pyc, *.db*, *.csv, .venv/ |
| `templates/` | 13 templates | HTML |
| `static/styles.css` | ~15 KB | Dark Bootstrap theme |

---

## 22. Consistency notes & known pitfalls

- **Settled decision (implemented) — web escalation trigger for `small_agency_scaling`**: the question "extend the web search to confident agencies" (asked when validating the `confidence < 0.7` net) is SETTLED and coded: the 3rd block of the trigger (pipeline.py:250-258) adds `segment == small_agency_scaling AND hiring_technical AND confidence >= 0.7` TO the existing net, without replacing it. Locked by a dedicated test (test_web_escalation_trigger.py) covering both OR branches + the negatives. Do not "simplify" this trigger into an exclusive OR.
- **Erroneous docstring**: `app.py:4` mentions "Claude scoring" while the scoring uses Groq (`llama-3.3-70b-versatile`) — to fix.
- **Broken README**: unclosed ```bash block in the Installation section (the rest of the Markdown rendering is affected).
- **`__pycache__` remnants**: `pipeline_phase2.cpython-311.pyc` and `test_stop_analysis.cpython-311.pyc` without a corresponding source (deleted files).
- **TODO pricing/careers extractors**: in the readable export, `pricing_preview`/`careers_preview` remain raw text; the dedicated extractors ("self-serve vs sales-only", "N engineering jobs") are to code (export.py:323-326).
- **`executescript` and semicolons**: the `;` split requires any block containing `;` (PL/pgSQL functions, triggers) to be passed as a standalone statement.
- **Legacy sessions** (owner_id NULL): visible only to the admin (`_assert_session_access`).
- **Role in signed session**: a role change in the DB only takes effect after re-login.
- **In-memory progress**: `_pipeline_progress` lives in the process — an app restart during a run loses the progress (the DB statuses, however, are preserved).
- **Firecrawl free tier**: default 15 s throttle in sequential mode; in multi-key mode, max 1 parallel request per key.
- **Git**: 13+ commits on main; the latest `cecc9b3` fixes the site_content_missing/status contradiction and splits the FETCH_FAILED (dead site) vs FETCH_PARTIAL (homepage OK, sub-pages failed) semantics. Warning: ~17 modified files uncommitted (reskin, triggers, renumbering, per-user numbering).