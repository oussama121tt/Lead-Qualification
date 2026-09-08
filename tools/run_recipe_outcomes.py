"""Manually record send/reply outcomes for saved Apollo recipes (Option B).

There is no Apollo analytics write-back wired up yet (that's a follow-up once
write access is confirmed), so `sent`/`replies` per recipe are entered here —
either for one recipe, or for all recipes at once with the same numbers. This
feeds recipes.record_outcomes so the recipe UI's Sent/Replies/Reply-rate are
backed by intentional data rather than silently blank.

Usage (from cron or on demand):
    .venv/bin/python tools/run_recipe_outcomes.py --all --sent 42 --replies 3
    .venv/bin/python tools/run_recipe_outcomes.py --recipe 5 --sent 12 --replies 2
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db as dbmod
import recipes as recipesmod


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--recipe", type=int, help="recipe id to update")
    target.add_argument("--all", action="store_true", help="update every recipe")
    parser.add_argument("--sent", type=int, default=0, help="delta to add to sent")
    parser.add_argument("--replies", type=int, default=0, help="delta to add to replies")
    args = parser.parse_args()

    if args.sent < 0 or args.replies < 0 or args.replies > args.sent:
        print("error: sent/replies must be >= 0 and replies <= sent")
        return 2

    conn = dbmod.get_connection()
    try:
        if args.all:
            recipes = recipesmod.list_all(conn)
            if not recipes:
                print("[outcomes] no recipes to update.")
                return 0
            for r in recipes:
                recipesmod.record_outcomes(conn, r["id"], sent=args.sent, replies=args.replies)
                print(f"  {r['id']:>4}  {r['name']:<32} +sent={args.sent} +replies={args.replies}")
            print(f"[outcomes] updated {len(recipes)} recipe(s).")
        else:
            r = recipesmod.get(conn, args.recipe)
            if r is None:
                print(f"error: recipe {args.recipe} not found")
                return 1
            recipesmod.record_outcomes(conn, args.recipe, sent=args.sent, replies=args.replies)
            print(f"[outcomes] recipe {args.recipe} ({r['name']}): sent={r['sent']+args.sent} replies={r['replies']+args.replies}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
