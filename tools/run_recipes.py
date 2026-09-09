"""Scheduled Apollo recipe runner (Task 13: saved filter recipes, scheduled).

Runs every saved recipe (or a named subset) through sourcing.run_recipe —
search (free), Stage-0 pre-filter (free), credit-gated enrich, insert —
and logs the yield. Intended to run from cron / a scheduled cloud job:

    .venv/bin/python tools/run_recipes.py --all            # every saved recipe
    .venv/bin/python tools/run_recipes.py --recipe 3       # one recipe
    .venv/bin/python tools/run_recipes.py --all --dry-run  # report, spend 0

The credit governor inside run_recipe enforces the hard monthly cap and
reports a per-run cost estimate before any credit is spent; a run that would
breach the cap raises ApolloCreditCapReached and that recipe is skipped.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import recipes as recipesmod
import sourcing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--recipe", type=int, help="single recipe id to run")
    target.add_argument("--all", action="store_true", help="run every saved recipe")
    parser.add_argument("--dry-run", action="store_true",
                        help="stop after the pre-filter; report credits_needed, spend 0")
    args = parser.parse_args()

    import db as dbmod
    conn = dbmod.get_connection()
    try:
        ids = []
        if args.all:
            ids = [r["id"] for r in recipesmod.list_all(conn)]
        else:
            if recipesmod.get(conn, args.recipe) is None:
                print(f"error: recipe {args.recipe} not found")
                return 1
            ids = [args.recipe]

        if not ids:
            print("[recipes] no saved recipes to run.")
            return 0

        ran = skipped = 0
        for rid in ids:
            try:
                summary = sourcing.run_recipe(conn, recipe_id=rid, dry_run=args.dry_run)
            except Exception as e:  # e.g. ApolloCreditCapReached / ApolloError
                print(f"[recipes] recipe {rid} SKIPPED: {e}")
                skipped += 1
                continue
            ran += 1
            mode = "DRY-RUN" if args.dry_run else "run"
            print(f"[recipes] recipe {rid}: {mode} pulled={summary['pulled']} "
                  f"to_enrich={summary['to_enrich']} credits_needed={summary['credits_needed']} "
                  f"credits_used={summary.get('credits_used_this_month')}/{summary.get('monthly_cap')}")
        print(f"[recipes] done: {ran} ran, {skipped} skipped.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
