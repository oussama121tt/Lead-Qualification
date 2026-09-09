"""Task 13 nightly — pull Apollo outbound analytics into lead_outcomes.

Fetches the last `--days` days of Apollo outreach emails (Search for Outreach
Emails endpoint), appends them to the apollo_analytics_sync_report raw table,
then folds each into lead_outcomes by matching leads.email. This is the
nightly population Task 16's Signal→Outcome attribution screen reads from.

Requires APOLLO_API_KEY in .env (added manually by the operator).

Run from cron / a scheduled cloud job:
    .venv/bin/python tools/run_outcomes_sync.py --days 1
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db as dbmod
import apollo_analytics as apollo_analyticsmod


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=1, help="lookback window in days")
    args = parser.parse_args()
    if args.days < 1:
        print("error: --days must be >= 1")
        return 2

    conn = dbmod.get_connection()
    try:
        result = apollo_analyticsmod.sync_analytics_report(conn, days=args.days)
    except apollo_analyticsmod.ApolloAnalyticsError as e:
        print(f"[outcomes-sync] error: {e}")
        return 1
    finally:
        conn.close()

    print(f"[outcomes-sync] {result['month']}: {result['messages']} messages, "
          f"{result['matched']} matched to leads, {result['replied']} replies, "
          f"{result['opened']} opened, {result['clicked']} clicked, "
          f"{result['unmatched']} unmatched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())