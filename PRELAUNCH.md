# Pre-Launch Gates — Trigger Monitoring (Task 9)

These are operational steps with no corresponding test — nothing in the suite
will fail if they're skipped, so they must be checked manually before this
feature is live in production.

- [ ] Replace the placeholder `sgai_monthly_credit_cap` (config.toml, currently
      1000) with a real number sized against actual ScrapeGraphAI plan
      quota/pricing.
- [ ] Manually create the Render Cron Job for `tools/run_triggers.py` (see
      DEVELOPER_TASKS.md for the exact command/schedule/env vars) — nothing
      currently invokes this script; `[triggers].enabled` being true does
      NOT mean it's running.

Add entries here for any future task with a real-world action a test can't
verify.