# Task 4 Neon validation

Status: blocked pending credential rotation.

Date recorded: 2026-09-04.

The repository contains credentials that were exposed during the audit session. A real Neon connection and live scoring/export validation must not be run with those credentials. No remote database query was executed during this validation.

Once the credentials have been revoked and replaced, run the following checks against a test lead or an isolated validation lead:

1. Run `db.init_db(conn)` and confirm `lead_scores.sensitive_data_categories` is `TEXT` and `lead_scores.data_sensitivity_score` is `INTEGER`.
2. Save a verdict containing `sensitive_data_categories=["minors"]` and `data_sensitivity_score=72`.
3. Read the row directly from `lead_scores` and confirm the JSON text and integer value.
4. Generate `scores.csv` and confirm both columns contain the expected values.
5. Run `python -m pytest tests -q` and `python tools/run_golden.py --min-agreement 0.8`.

The local DB binding check is already covered by the test suite without contacting Neon.

## Closing this block

The Neon validation should be performed after the exposed credentials have been revoked
and replaced. The campaign fixtures are not stored in this repository, so the 30-50 case
golden-set target cannot be completed honestly from the current checkout. To close it:

- request the frozen Apollo/scraper exports and manually verified verdicts from the person
	who owns the campaign data; or
- capture the scraper output and human-reviewed verdict for each new production lead,
	excluding secrets and personal data that is not required for scoring, until the set
	reaches 30-50 diverse cases.

Record the source, review date, and reviewer for each added case. Do not replace missing
campaign data with synthetic fixtures and do not run the Neon check before credential rotation.
