"""Trigger monitoring scheduler.

Re-checks scored leads whose `next_check_at <= now`: runs the tier-appropriate
trigger checks (cheap checks weekly, the expensive LinkedIn check only for
high-scoring leads), writes fired events to lead_trigger_events, bumps the
lead's trigger_priority + trigger_hook (surfaced at the top of the review
queue) and sets the next check time.

Expected to run from cron / a scheduled cloud job:
    .venv/bin/python tools/run_triggers.py
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db as dbmod
import triggers as triggersmod
from runconfig import load_config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="only list due leads, run no checks")
    parser.add_argument("--limit", type=int, default=0,
                        help="override [triggers].max_leads_per_run")
    args = parser.parse_args()

    cfg = load_config()
    if not cfg.triggers.enabled:
        print("[triggers] disabled ([triggers].enabled = false); nothing to do.")
        return 0

    limit = args.limit or cfg.triggers.max_leads_per_run
    now = _now_iso()

    conn = dbmod.get_connection()
    try:
        due = dbmod.get_due_leads(conn, now=now, limit=limit)
        if args.dry_run:
            print(f"[triggers] {len(due)} due lead(s) at {now} (dry run):")
            for lead in due:
                print(f"  {lead.get('id'):>6}  {lead.get('company_name') or lead.get('website_url') or ''}")
            return 0
        summary = triggersmod.run_due_leads(conn, due, cfg, now=now)
    finally:
        conn.close()

    print(f"[triggers] run at {now}")
    print(f"  due        : {len(due)}")
    print(f"  checked    : {summary['checked']}")
    print(f"  fired      : {summary['fired']}")
    for ev in summary["events"]:
        print(f"    - {ev['trigger']:<20} lead {ev['lead_id']} — {ev['detail']}")
    paused = summary["budget_paused"]
    if paused["apollo"] or paused["sgai"]:
        print(f"  paused     : Apollo budget exhausted ({paused['apollo']} lead(s)), "
              f"SGAI budget exhausted ({paused['sgai']} lead(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())