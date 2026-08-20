# Merge: lead_tool operational layer → Lead Qualification Engine

This branch (`merge-lead-tool`) merges the proven operational modules of the
internal `lead_tool` enrichment project into the main product, plus the fixes
from the last code review. Everything is offline-tested (`python -m pytest
tests -q` — 30 tests, no DB server or API key needed).

## New modules (ported from lead_tool, adapted)

| Module | What it brings |
|---|---|
| `keyring.py` | API-key rotation with a recovering cooling pool: 429 → brief cool + retry same key first; 401/402/403 (out of credits) → long cool; automatic recovery; fingerprint-only logging. |
| `caps.py` | GLOBAL daily/weekly LinkedIn caps (default 50/day, 250/week) persisted in the DB across all runs/sessions. `reserve()` gates, `record_done()` only counts SUCCESSFUL profiles. |
| `throttle.py` | Human-mimicking pacing (randomized 45–180s per profile + 15–40min long pause every 15–20 profiles) and interruptible sleeps. |
| `linkedin_lane.py` | Founder LinkedIn deep harvest: full profile + each activity permalink fetched for FULL post text, with **code-enforced authored-vs-liked attribution** (handle match, never an LLM guess), junk filtering, bio-post force-keep, post caps. Sequential process-wide, paced, capped. Feeds the scorer as `person_linkedin` evidence with explicit AUTHORED/liked labels. |
| `site_fetcher.py` | FREE-FIRST website fetching (requests + BeautifulSoup, zero credits) with JS-shell detection and `<blockquote>` → `"> "` preservation so the testimonial tagger keeps working. |
| `costlog.py` | FR-7 at last: every LLM call logged (`llm_calls` table — tokens in/out, latency, estimated USD) + a per-session hard budget cap. |
| `runconfig.py` + `config.toml` | All operational tuning in one file, with a `[fast]` test overlay (`RUN_MODE=fast`): tiny delays + caps bypassed to verify the pipeline in minutes without spending the real budget. |
| `tests/` | 30 offline tests: attribution regressions, caps gating, key rotation, cost/budget, JS-shell detection, config overlay, scorer guards (verdict validation, quote/hook grounding). |

## Changes to existing modules

- **scraper.py** — `scrape_website()` is now hybrid: free fetch first,
  Firecrawl only for JS-heavy pages (per page, not per site). Cuts paid
  scraping by an order of magnitude on server-rendered sites. Every result
  carries `fetch_notes` saying how each page was fetched. Disable with
  `[website] free_first=false`.
- **pipeline.py** — per-lead **coverage notes** (`leads.coverage_notes`):
  which evidence lanes ran, what was capped/failed/thin — nothing silent
  anymore. Session **budget gate** before each lead (cancels the session
  cooperatively at the cap). Web escalation now uses the LinkedIn deep
  harvest when a founder profile URL is known (CSV `linkedin_url` column —
  newly ingested — or the site's own single `/in/` link), with the snippet
  search as fallback.
- **scorer.py** — LLM calls go through `llm_provider` (switch to Claude with
  `SCORING_LLM_PROVIDER=anthropic`, model per the original spec:
  `claude-sonnet-4-6`). `cost_cb` logs every call, retries included.
- **emailer.py** — sender signature from `.env` (`SENDER_NAME`,
  `SENDER_COMPANY`) instead of a hardcoded person; mandatory opt-out line
  appended to every email (compliance); calls logged to `llm_calls`.
- **app.py** — email generation AND sending now run in **background
  threads** (they previously ran inside the HTTP request and would be killed
  by gunicorn's 30s worker timeout after ~3 emails); the review page polls
  `/session/<id>/email_job` and refreshes when done. Email drafting is
  **gated to 'Ready to approve' leads** (spec hard rule; skips are counted
  and reported). XSS fixed (`_table_html` escapes all data cells). Secret
  key fallback is now random-per-process instead of a guessable constant.
  `lead_review_view` fetches one lead by id instead of scanning the whole
  table. Results page shows the session's running LLM spend.
- **db.py** — new tables `llm_calls`, `li_daily_counter`; new lead columns
  `linkedin_url` (ingested from Apollo CSV when present) and
  `coverage_notes`; helpers `get_lead_with_score`, `append_coverage_notes`,
  `get_coverage_notes`.
- **requirements.txt** — version floors pinned; added `beautifulsoup4`,
  `lxml`, `anthropic`, `pytest`.

## New .env knobs (all optional)

```
RUN_MODE=fast                 # test overlay: tiny delays, caps bypassed
SCORING_LLM_PROVIDER=groq     # or anthropic (needs ANTHROPIC_API_KEY)
EMAIL_LLM_PROVIDER=groq       # or anthropic
ANTHROPIC_MODEL=claude-sonnet-4-6
SENDER_NAME=Wael              # email signature (SENDER_COMPANY defaults to RuyaTech)
FLASK_SECRET_KEY=<random>     # set in production for stable sessions
```

## Still open (not in this merge)

- Instantly/Smartlead CSV export with `{{first_line}}` variables (the
  alternative to Gmail sending for volume).
- Approve/Reject buttons wired to `review_lead` on the per-lead review page
  (the endpoint exists; the auto-categorized "Ready to approve" bucket now
  gates emailing, but explicit per-lead human approval is still the spec's
  intent).
- Multi-account / warmed-domain sending for real volume.
