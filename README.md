# Lead Qualification & Scoring Engine

Complete pipeline: **Ingestion → Deduplication → Scraping (Firecrawl) → AI Scoring (Groq)**, with a Flask web interface.

## Installation

```bash
pip install -r requirements.txt
# Create .env with FIRECRAWL_API_KEY and GROQ_API_KEY
```

## Launch the interface

```bash
python app.py
```

A PostgreSQL database is used automatically via `DATABASE_URL` (Neon). Each import creates a separate analysis session.

## Interface features

1. **CSV upload** — import an Apollo CSV into PostgreSQL (columns: first_name, last_name, title, company_name, email, website_url).
2. **Full analysis** — ingestion + dedup + Firecrawl scraping + Groq (llama-3.3-70b-versatile) scoring, in one click.
3. **Independent actions** — import only, dedup only, pipeline only.
4. **Tables** — raw leads view and scored leads view with segment filters.
5. **Human review** — approve/reject a lead, change its segment.
6. **Downloads** — raw scraping CSV and scoring CSV.
7. **Lead details** — evidence_quotes, personalization_hooks, disqualify_reason.
8. **History** — select past sessions to review their results.

## Files

| File | Role |
|---|---|
| `db.py` | PostgreSQL schema + CRUD helpers (leads, sessions, scores, exports) |
| `dedup.py` | 3-level deduplication (exact email, domain, fuzzy name via RapidFuzz) |
| `scraper.py` | Firecrawl scraping + extraction of deterministic technical signals |
| `scorer.py` | Groq scoring: evaluates each lead and produces a structured JSON verdict |
| `pipeline.py` | Orchestrator: chains scraping + scoring lead by lead, isolates failures |
| `export.py` | CSV export: raw scraping, scores, and readable format for human review |
| `app.py` | Complete Flask interface |

## Scoring segments

| Segment | Description | Recommended offer |
|---|---|---|
| `ai_solo_founder` | Non-technical founder building with AI (MAIN TARGET) | `ai_audit` |
| `technical_founder` | Technical team, uses AI as a dev tool | `general_audit` |
| `small_agency_scaling` | Agency / studio in a scaling phase | `pipeline` |
| `too_big` | Established company, far above the target persona | `none` |
| `wrong_field` | Unrelated sector | `none` |
| `unclear` | Insufficient evidence | `none` |

## Lead statuses

`NEW` → `PARSED` / `FETCH_PARTIAL` / `FETCH_FAILED` → `SCORED` / `LOW_CONFIDENCE` / `SCORE_FAILED` → `APPROVED` / `REJECTED`
