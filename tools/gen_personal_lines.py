"""Generate the Personal Line for leads, store it locally, send nothing.

The Apollo sequence copy is already written; only the line after the greeting
changes per contact. This writes that line to leads.personal_line together
with the citation it rests on and a status:

    ok            grounded and in house style -> safe to send personalised
    empty         the model judged the evidence too thin -> control arm
    rejected:...  failed a guard (too long, banned phrase, ungrounded)

Nothing here touches Apollo. Review the lines in the sheet, then enrol.

    python tools/gen_personal_lines.py --approved            # review-queue approved leads
    python tools/gen_personal_lines.py --strong --limit 50   # confidence >= 0.8 targets
    python tools/gen_personal_lines.py --session 12 --redo   # regenerate, ignoring stored lines
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db as dbmod
import personal_line as plmod
import pipeline as pipelinemod
import scorer
from profile import load_profile

TARGET = tuple(sorted(load_profile().target_segments))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--approved", action="store_true", help="leads marked APPROVED in the review queue")
    g.add_argument("--strong", action="store_true", help="target segments at confidence >= 0.8")
    g.add_argument("--session", type=int, help="every target-segment lead in one session")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--redo", action="store_true", help="regenerate even where a line is stored")
    args = ap.parse_args()

    where = ["l.is_duplicate = 0",
             f"s.segment IN ({','.join('?' * len(TARGET))})"]
    params: list = list(TARGET)
    if args.approved:
        where.append("l.review_status = 'APPROVED'")
    elif args.strong:
        where.append("COALESCE(s.confidence, 0) >= 0.8")
    else:
        where.append("l.session_id = ?")
        params.append(args.session)
    if not args.redo:
        where.append("(l.personal_line_status IS NULL OR l.personal_line_status = '')")

    conn = dbmod.get_connection()
    try:
        rows = [dict(r) for r in conn.execute(f"""
            SELECT l.*, s.segment, s.confidence, s.recommended_offer, s.sensitive_data_categories,
                   s.built_with_ai_signals, s.technical_signals, s.pain_signals, s.evidence_quotes,
                   s.personalization_hooks, s.founder_profile, s.build_evidence
            FROM leads l
            JOIN lead_scores s ON s.id = (SELECT MAX(id) FROM lead_scores WHERE lead_id = l.id)
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(s.confidence, 0) DESC, l.id
        """, params).fetchall()]
        if args.limit:
            rows = rows[: args.limit]
        print(f"[lines] {len(rows)} lead(s) to generate", flush=True)
        stats: dict[str, int] = {}
        t0 = time.monotonic()
        for i, r in enumerate(rows, 1):
            content = dbmod.get_lead_content(conn, r["id"])
            site = scorer.rows_to_text(content, max_chars=8000)
            web = scorer._format_web_search_evidence(pipelinemod._load_persisted_web_evidence(conn, r["id"]))
            lead = dict(r)
            lead["_metadata"] = pipelinemod._build_lead_metadata(r)
            cost_cb = pipelinemod._make_cost_cb(conn, r.get("session_id"), r["id"], "personal_line")
            try:
                out = plmod.generate(lead, r, site, web, cost_cb=cost_cb)
            except Exception as exc:
                out = {"line": "", "based_on": "", "status": f"rejected:error:{exc.__class__.__name__}"}
            conn.execute(
                "UPDATE leads SET personal_line = ?, personal_line_based_on = ?, personal_line_status = ? WHERE id = ?",
                (out["line"] or None, out["based_on"] or None, out["status"], r["id"]),
            )
            conn.commit()
            stats[out["status"].split(":")[0]] = stats.get(out["status"].split(":")[0], 0) + 1
            print(f"[{time.monotonic()-t0:5.0f}s] {i:>4}/{len(rows)} {str(r['company_name'])[:26]:<26} "
                  f"{out['status']:<22} {out['line'][:90]}", flush=True)
        print(f"[lines] done in {time.monotonic()-t0:.0f}s | {stats}")
        usable = stats.get("ok", 0)
        print(f"[lines] {usable} usable line(s); the rest run the control sequence with no line.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
