"""Bulk sourcing run — work order "Bulk sourcing run, evaluation-ready export".

    python tools/run_bulk_sourcing.py --plan            # FREE: search + prefilter every recipe, print yield table, spend 0
    python tools/run_bulk_sourcing.py --enrich narrow   # spend credits on narrow recipes (within run_credit_cap)
    python tools/run_bulk_sourcing.py --enrich broad    # then the broad sweeps
    python tools/run_bulk_sourcing.py --score           # scrape + score every unscored lead in this run's sessions
    python tools/run_bulk_sourcing.py --status          # where are we

Pipeline order enforced (never deviates): search (free) -> Stage-0 prefilter
(free) -> DNC -> credit gate -> enrich -> insert -> [score] -> escalate if
qualified -> store. Nothing is emailed by this tool.

State is persisted in bulk_run_state.json next to this file so a crash or a
credit-cap stop resumes exactly where it left off. Every recipe becomes an
apollo_recipes row (yield tracked) and one analysis_session (label prefixed
"BULK:") so the export can find everything by session label.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("PYTHONUTF8", "1")

import apollo_client
import db as dbmod
import dnc as dncmod
import prefilter as prefiltermod
import recipes as recipesmod
import sourcing
from runconfig import load_config

RECIPES_PATH = HERE.parent / "golden" / "bulk_recipes.json"
STATE_PATH = HERE / "bulk_run_state.json"
REJECTS_PATH = HERE.parent / "exports" / "bulk_prefilter_rejects.jsonl"


def _load_recipes() -> dict:
    return json.loads(RECIPES_PATH.read_text(encoding="utf-8"))


def _state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"started_at": None, "credits_spent": 0, "recipes": {}, "sessions": []}


def _save(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


def _recipe_name(kind: str, kw: str) -> str:
    return f"bulk-{kind}-{kw.replace(' ', '-')}"


def _ensure_recipe(conn, kind: str, kw: str, base: dict) -> int:
    name = _recipe_name(kind, kw)
    existing = [r for r in recipesmod.list_all(conn) if r["name"] == name]
    if existing:
        return existing[0]["id"]
    return recipesmod.create(conn, name, {**base, "q_keywords": kw})


def cmd_plan(args):
    """FREE pass: search + prefilter each recipe, no enrichment. Prints yield
    table and writes prefilter rejects (with reasons) for the evaluation file."""
    cfg = load_config()
    spec = _load_recipes()
    base = spec["base"]
    conn = dbmod.get_connection()
    REJECTS_PATH.parent.mkdir(exist_ok=True)
    rej_f = open(REJECTS_PATH, "w", encoding="utf-8")
    dnc_emails, dnc_domains = dncmod.load_sets(conn)
    rows = []
    reasons = Counter()
    for kind in ("narrow", "broad"):
        for kw in spec[kind]:
            filters = {**base, "q_keywords": kw}
            max_people = spec.get("broad_max_people", 1500) if kind == "broad" else cfg.apollo.max_people_per_run
            try:
                people = apollo_client.search_people_all(filters, max_people=max_people, per_page=cfg.apollo.search_page_size)
            except Exception as e:
                rows.append((kind, kw, "ERR", 0, 0, str(e)[:60])); continue
            pf = prefiltermod.prefilter_people(people, max_headcount=cfg.prefilter.max_headcount,
                                               min_headcount=cfg.prefilter.min_headcount, use_llm=False)
            kept = [p for p in pf["keep"]
                    if not dncmod.check_lead(None, (p.get("organization") or {}).get("primary_domain"), dnc_emails, dnc_domains)]
            for p, why in pf["reject"]:
                reasons[why.split(":")[0]] += 1
                rej_f.write(json.dumps({"recipe": _recipe_name(kind, kw), "first_name": p.get("first_name"),
                                        "last_name_obfuscated": p.get("last_name_obfuscated"), "title": p.get("title"),
                                        "company": (p.get("organization") or {}).get("name"), "reject_reason": why},
                                       ensure_ascii=False) + "\n")
            rows.append((kind, kw, len(people), len(kept), len(pf["reject"]), ""))
            print(f"  {kind:6s} {kw:22s} pulled={len(people):4d} keep={len(kept):4d} reject={len(pf['reject']):4d}", flush=True)
    rej_f.close()
    total_keep = sum(r[3] for r in rows if r[2] != "ERR")
    print("\n=== PLAN (0 credits spent) ===")
    print(f"recipes: {len(rows)} | pulled: {sum(r[2] for r in rows if r[2] != 'ERR')} | would enrich: {total_keep} | rejected: {sum(r[4] for r in rows if r[2] != 'ERR')}")
    print("top reject reasons:", reasons.most_common(6))
    apollo_client.ensure_usage_table(conn)
    print(f"credits used this month: {apollo_client.credits_used_this_month(conn)} / {cfg.apollo.monthly_credit_cap} | run cap: {cfg.apollo.run_credit_cap}")
    print(f"rejects written: {REJECTS_PATH}")
    conn.close()


def cmd_enrich(args):
    """Spend credits: run the recipes of the chosen class through sourcing.run_recipe
    (search -> prefilter -> DNC -> credit gate -> enrich -> insert), stopping cleanly
    at the run credit cap."""
    cfg = load_config()
    spec = _load_recipes()
    base = spec["base"]
    state = _state()
    state["started_at"] = state["started_at"] or time.strftime("%Y-%m-%dT%H:%M:%S")
    conn = dbmod.get_connection()
    kinds = ["narrow", "broad"] if args.enrich == "all" else [args.enrich]
    for kind in kinds:
        for kw in spec[kind]:
            name = _recipe_name(kind, kw)
            if state["recipes"].get(name, {}).get("done"):
                continue
            if cfg.apollo.run_credit_cap and state["credits_spent"] >= cfg.apollo.run_credit_cap:
                print(f"RUN CREDIT CAP REACHED ({state['credits_spent']}/{cfg.apollo.run_credit_cap}) — stopping cleanly before '{name}'.")
                _save(state); conn.close(); return
            rid = _ensure_recipe(conn, kind, kw, base)
            if kind == "broad":
                # broad sweeps page deep; temporarily widen the per-run ceiling
                cfg.apollo.max_people_per_run = spec.get("broad_max_people", 1500)
            try:
                dry = sourcing.run_recipe(conn, recipe_id=rid, dry_run=True)
                need = dry["to_enrich"]
                room = (cfg.apollo.run_credit_cap - state["credits_spent"]) if cfg.apollo.run_credit_cap else need
                if need == 0:
                    state["recipes"][name] = {"done": True, "pulled": dry["pulled"], "enriched": 0, "session_id": None}
                    _save(state); print(f"  {name:36s} pulled={dry['pulled']:4d} -> nothing to enrich"); continue
                if need > room:
                    print(f"  {name:36s} needs {need} credits, only {room} left in run cap — stopping cleanly.")
                    _save(state); conn.close(); return
                res = sourcing.run_recipe(conn, recipe_id=rid, label=f"BULK:{name}")
                state["credits_spent"] += res["credits_spent"]
                state["recipes"][name] = {"done": True, "pulled": res["pulled"], "enriched": res["enriched"],
                                          "inserted": res["inserted"], "session_id": res["session_id"],
                                          "prefilter": res["prefilter"]}
                if res["session_id"]:
                    state["sessions"].append(res["session_id"])
                _save(state)
                print(f"  {name:36s} pulled={res['pulled']:4d} enriched={res['enriched']:3d} credits={res['credits_spent']:3d} session={res['session_id']}  (run total {state['credits_spent']})", flush=True)
            except apollo_client.ApolloCreditCapReached as e:
                print(f"MONTHLY CREDIT CAP: {e} — stopping cleanly."); _save(state); conn.close(); return
            except Exception as e:
                state["recipes"][name] = {"done": False, "error": str(e)[:200]}
                _save(state); print(f"  {name:36s} ERROR {str(e)[:120]}")
    conn.close()
    print(f"\nenrich pass '{args.enrich}' complete. credits spent this run: {state['credits_spent']}")


def cmd_score(args):
    """Scrape + score every unscored lead in this run's sessions (sequential
    sessions, concurrent leads). Resumable: only NOT_YET_SCORED leads run."""
    import pipeline as pipelinemod
    state = _state()
    conn = dbmod.get_connection()
    sessions = list(dict.fromkeys(state["sessions"]))
    if not sessions:
        print("no sessions in state — run --enrich first"); return
    t0 = time.monotonic()
    for sid in sessions:
        pending = dbmod.get_leads_to_process(conn, session_id=sid)
        if not pending:
            continue
        print(f"\n== session {sid}: {len(pending)} lead(s) to score ==", flush=True)
        dbmod.update_analysis_session_status(conn, sid, "running")
        done = 0
        for ev in pipelinemod.run_pipeline(conn, throttle_seconds=1, session_id=sid, concurrency=args.concurrency):
            if ev.get("step") == "done":
                done += 1
                v = ev.get("verdict") or {}
                print(f"  [{time.monotonic()-t0:6.0f}s] {ev.get('company_name','')[:26]:<26} {ev.get('status'):<15} {str(v.get('segment')):<20} conf={v.get('confidence')}", flush=True)
        dbmod.update_analysis_session_status(conn, sid, "completed", completed_at=dbmod._now())
    conn.close()
    print(f"\nscoring complete in {time.monotonic()-t0:.0f}s")


def cmd_status(args):
    state = _state()
    print(json.dumps({k: v for k, v in state.items() if k != "recipes"}, indent=1))
    done = sum(1 for r in state["recipes"].values() if r.get("done"))
    print(f"recipes done: {done}/{len(state['recipes'])} | credits spent this run: {state['credits_spent']}")
    for name, r in state["recipes"].items():
        print(f"  {name:36s} {r}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--enrich", choices=["narrow", "broad", "all"])
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--concurrency", type=int, default=3)
    a = ap.parse_args()
    if a.plan: cmd_plan(a)
    elif a.enrich: cmd_enrich(a)
    elif a.score: cmd_score(a)
    elif a.status: cmd_status(a)
    else: ap.print_help()
