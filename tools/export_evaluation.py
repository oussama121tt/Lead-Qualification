"""Evaluation-ready export — work order §5–6.

    python tools/export_evaluation.py                # all BULK:* sessions
    python tools/export_evaluation.py --sessions 1 5 # explicit session ids
    python tools/export_evaluation.py --all          # every session in the DB

Writes to ./exports/:
  leads_core.csv      one row per lead that survived the prefilter (identity, Apollo,
                      employment history JSON, verdict, signals, new fields, evidence,
                      surface scan, provenance) — everything except the long text fields
  leads_content.csv   lead_id + the raw text the model saw (site excerpt ~3000 chars,
                      authored LinkedIn posts ~2000 chars) — joinable on lead_id
  prefilter_rejects.csv  people dropped by Stage-0 with the reject reason
                      (from tools/bulk_prefilter_rejects.jsonl written by --plan)
  run_summary.md      one-page run summary (§6)

Nothing here truncates employment_history; text fields are excerpted only in
leads_content.csv as the work order specifies.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("PYTHONUTF8", "1")

import costlog
import db as dbmod
from constants import TARGET_SEGMENTS

OUT_DIR = HERE.parent / "exports"
REJECTS_JSONL = HERE / "bulk_prefilter_rejects.jsonl"

CORE_FIELDS = [
    # Identity & Apollo
    "lead_id", "first_name", "last_name", "title", "seniority", "headline", "email",
    "email_status", "linkedin_url", "company_name", "company_domain", "company_website",
    "employees", "founded_year", "industry", "headcount_growth_6m", "headcount_growth_12m",
    "revenue", "org_keywords", "location", "country",
    # Employment history (full, JSON)
    "employment_history",
    # Scoring output
    "segment", "confidence", "needs_human_review", "recommended_offer", "disqualify_reason",
    # Signals
    "app_builder_fingerprint", "site_builder_fingerprint", "on_builder_subdomain",
    "tech_stack", "traction_signals",
    # New fields
    "sensitive_data_categories", "data_sensitivity_score", "budget_signal",
    "budget_evidence", "budget_blockers",
    # Evidence
    "hooks", "evidence_quotes", "pain_signals", "technical_evidence", "built_with_ai_evidence",
    # Content meta
    "web_meta_title", "web_meta_description", "pages_fetched", "fetch_status",
    "li_authored_count", "li_status",
    # Surface scan
    "public_findings", "public_findings_count",
    # Provenance
    "recipe_name", "sourced_at", "apollo_credits_used", "scoring_model", "coverage_notes",
]
CONTENT_FIELDS = ["lead_id", "web_text_excerpt", "li_authored_posts_excerpt"]


def _j(v):
    """JSON-encode lists/dicts for a CSV cell; pass strings through; parse
    JSON-looking strings first so nested JSON is not double-encoded."""
    if v is None:
        return ""
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in "[{":
            try:
                return json.dumps(json.loads(s), ensure_ascii=False)
            except Exception:
                return v
        return v
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v


def _parse(v):
    if isinstance(v, str) and v.strip()[:1] in "[{":
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _hook_source(based_on: str, site_text: str, web_text: str) -> str:
    b = (based_on or "").strip().strip('"“”').lower()
    if b and b in (site_text or "").lower():
        return "site"
    if b and b in (web_text or "").lower():
        return "web"
    return "unverified"


def _select_sessions(conn, args) -> list[int]:
    if args.sessions:
        return [int(s) for s in args.sessions]
    rows = conn.execute("SELECT id, label FROM analysis_sessions ORDER BY id").fetchall()
    if args.all:
        return [r["id"] for r in rows]
    return [r["id"] for r in rows if (r["label"] or "").startswith("BULK:")]


def export(conn, session_ids: list[int]) -> dict:
    OUT_DIR.mkdir(exist_ok=True)
    core_rows, content_rows = [], []
    stats = {"leads": 0, "verdicts": Counter(), "confidence": [], "review_reasons": Counter(),
             "fetch": Counter(), "scan_by_check": Counter(), "per_recipe": Counter(),
             "per_recipe_qualified": Counter(), "models": Counter(), "broken": []}

    # session -> label / recipe / created
    sess = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM analysis_sessions").fetchall()}
    recipe_names = {r["name"]: r for r in __import__("recipes").list_all(conn)}
    scoring_models = {}
    for r in conn.execute("SELECT lead_id, model FROM llm_calls WHERE purpose IN ('score','rescore')").fetchall():
        scoring_models[r["lead_id"]] = r["model"]

    for sid in session_ids:
        s = sess.get(sid) or {}
        label = s.get("label") or ""
        recipe_name = label[5:] if label.startswith("BULK:") else label
        leads = dbmod.get_leads_with_scores(conn, session_id=sid)
        for l in leads:
            if l.get("is_duplicate"):
                continue
            lid = l["id"]
            stats["leads"] += 1
            stats["per_recipe"][recipe_name] += 1
            ap = _parse(l.get("apollo_person")) or {}
            ao = _parse(l.get("apollo_org")) or {}
            tech = dbmod.get_lead_technical_signals(conn, lid) or {}
            content = dbmod.get_lead_content(conn, lid)
            site_text = "\n\n".join(f"## {c.get('source')}\n{c.get('content') or ''}" for c in content)
            home = next((c for c in content if c.get("source") == "homepage"), None)
            evidence = dbmod.get_lead_search_evidence(conn, lid)
            authored = []
            li_status = "not_run"
            web_text_parts = []
            for e in evidence:
                hits = e.get("results") or []
                for h in hits:
                    if not isinstance(h, dict):
                        continue
                    web_text_parts.append(h.get("content") or "")
                    if e.get("source") == "person_linkedin":
                        li_status = "ran"
                        if str(h.get("title", "")).startswith("AUTHORED"):
                            authored.append(h.get("content") or "")
            web_text = "\n".join(web_text_parts)
            notes = dbmod.get_coverage_notes(conn, lid)
            for n in notes:
                if "linkedin harvest capped" in n: li_status = "capped"
                elif "linkedin harvest unavailable" in n or "keys exhausted" in n: li_status = "no_credits"
                elif "harvest failed" in n: li_status = "failed"
            findings = [dict(r) for r in conn.execute(
                "SELECT * FROM lead_public_findings WHERE lead_id = ?", (lid,)).fetchall()]
            pf = [{"check": f.get("check") or f.get("check_name"), "severity": f.get("severity"),
                   "evidence_url": f.get("evidence_url"), "verified_at": f.get("verified_at")} for f in findings]
            for f in pf:
                stats["scan_by_check"][f["check"]] += 1

            hooks_raw = _parse(l.get("personalization_hooks")) or []
            hooks = []
            for h in hooks_raw if isinstance(hooks_raw, list) else []:
                if isinstance(h, dict):
                    hooks.append({"hook": h.get("hook"), "citation": h.get("based_on"),
                                  "source": _hook_source(h.get("based_on") or "", site_text, web_text)})
                else:
                    hooks.append({"hook": str(h), "citation": None, "source": "unverified"})

            seg = l.get("segment")
            stats["verdicts"][seg or "unscored"] += 1
            if l.get("confidence") is not None:
                stats["confidence"].append(float(l["confidence"]))
            if l.get("needs_human_review"):
                dr = (l.get("disqualify_reason") or "").lower()
                why = ("site_missing" if "site_content_missing" in dr else
                       "domain_mismatch" if "domain_mismatch" in dr else
                       "ungrounded" if "ungrounded" in dr else
                       "unclear" if seg == "unclear" else
                       "low_confidence" if (l.get("confidence") or 0) < 0.7 else "model_flag")
                stats["review_reasons"][why] += 1
            if seg in TARGET_SEGMENTS and not l.get("needs_human_review"):
                stats["per_recipe_qualified"][recipe_name] += 1
            stats["fetch"][l.get("status") if l.get("status") in ("FETCH_FAILED",) else ("fetched" if content else "no_content")] += 1
            stats["models"][scoring_models.get(lid, "?")] += 1
            if l.get("status") in ("SCORE_FAILED", "FETCH_FAILED"):
                stats["broken"].append(f"lead {lid} {l.get('company_name')}: {l.get('status')} {l.get('last_error') or ''}"[:160])

            loc = ", ".join(filter(None, [ap.get("city"), ap.get("country")]))
            core_rows.append({
                "lead_id": lid, "first_name": l.get("first_name"), "last_name": l.get("last_name"),
                "title": l.get("title"), "seniority": ap.get("seniority"), "headline": ap.get("headline"),
                "email": l.get("email"), "email_status": l.get("apollo_email_status"),
                "linkedin_url": l.get("linkedin_url"), "company_name": l.get("company_name"),
                "company_domain": ao.get("domain") or l.get("domain_normalized"),
                "company_website": l.get("website_url"), "employees": ao.get("employees"),
                "founded_year": ao.get("founded_year"), "industry": ao.get("industry"),
                "headcount_growth_6m": ao.get("headcount_growth_6m"),
                "headcount_growth_12m": ao.get("headcount_growth_12m"), "revenue": ao.get("revenue"),
                "org_keywords": _j(ao.get("keywords")), "location": loc, "country": ap.get("country"),
                "employment_history": _j([{"title": e.get("title"), "org": e.get("organization"),
                                           "start": e.get("start"), "end": e.get("end"),
                                           "is_current": e.get("current")} for e in (ap.get("employment_history") or [])]),
                "segment": seg, "confidence": l.get("confidence"),
                "needs_human_review": l.get("needs_human_review"),
                "recommended_offer": l.get("recommended_offer"), "disqualify_reason": l.get("disqualify_reason"),
                "app_builder_fingerprint": tech.get("app_builder_fingerprint") or tech.get("generator_fingerprint"),
                "site_builder_fingerprint": tech.get("site_builder_fingerprint"),
                "on_builder_subdomain": tech.get("on_builder_subdomain"),
                "tech_stack": _j({k: tech.get(k) for k in ("generator_meta_tag", "trend_fonts_found", "vibe_language_matches", "github_repo_url") if tech.get(k)}),
                "traction_signals": _j(tech.get("traction_signals")),
                "sensitive_data_categories": _j(l.get("sensitive_data_categories")),
                "data_sensitivity_score": l.get("data_sensitivity_score"),
                "budget_signal": l.get("budget_signal"), "budget_evidence": _j(l.get("budget_evidence")),
                "budget_blockers": _j(l.get("budget_blockers")),
                "hooks": _j(hooks), "evidence_quotes": _j(l.get("evidence_quotes")),
                "pain_signals": _j(l.get("pain_signals")), "technical_evidence": _j(l.get("technical_signals")),
                "built_with_ai_evidence": _j(l.get("built_with_ai_signals")),
                "web_meta_title": (home or {}).get("content", "")[:120].splitlines()[0] if home else "",
                "web_meta_description": "", "pages_fetched": len(content), "fetch_status": l.get("status"),
                "li_authored_count": len(authored), "li_status": li_status,
                "public_findings": _j(pf), "public_findings_count": len(pf),
                "recipe_name": recipe_name, "sourced_at": s.get("created_at"),
                "apollo_credits_used": 1 if (s.get("source_filename") == "apollo_api") else 0,
                "scoring_model": scoring_models.get(lid, ""), "coverage_notes": _j(notes),
            })
            content_rows.append({
                "lead_id": lid,
                "web_text_excerpt": site_text[:3000],
                "li_authored_posts_excerpt": ("\n---POST---\n".join(authored))[:2000],
            })

    with open(OUT_DIR / "leads_core.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CORE_FIELDS); w.writeheader(); w.writerows(core_rows)
    with open(OUT_DIR / "leads_content.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CONTENT_FIELDS); w.writeheader(); w.writerows(content_rows)

    rej_count, rej_reasons = 0, Counter()
    if REJECTS_JSONL.exists():
        with open(OUT_DIR / "prefilter_rejects.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["recipe", "first_name", "last_name_obfuscated", "title", "company", "reject_reason"])
            w.writeheader()
            for line in open(REJECTS_JSONL, encoding="utf-8"):
                r = json.loads(line); w.writerow(r); rej_count += 1
                rej_reasons[r["reject_reason"].split(":")[0]] += 1
    stats["rejects"] = rej_count; stats["reject_reasons"] = rej_reasons
    return stats


def summary_md(conn, session_ids, stats, elapsed_note: str) -> str:
    total_cost = sum(costlog.session_spend(conn, sid)["cost_usd"] for sid in session_ids)
    calls = sum(costlog.session_spend(conn, sid)["calls"] for sid in session_ids)
    import apollo_client
    credits = apollo_client.credits_used_this_month(conn)
    conf = stats["confidence"]
    buckets = Counter()
    for c in conf:
        buckets["<0.5" if c < 0.5 else "0.5-0.7" if c < 0.7 else "0.7-0.85" if c < 0.85 else ">=0.85"] += 1
    yield_rows = sorted(stats["per_recipe"].items(), key=lambda kv: -stats["per_recipe_qualified"][kv[0]])
    lines = [
        "# Bulk sourcing run — summary", "",
        f"- sessions: {len(session_ids)} | leads exported (survived prefilter): {stats['leads']} | prefilter rejects: {stats['rejects']}",
        f"- top reject reasons: {stats['reject_reasons'].most_common(5)}",
        f"- Apollo credits used this month: {credits} | LLM calls: {calls} | LLM cost: ${total_cost:.4f} | {elapsed_note}",
        f"- scoring model(s): {dict(stats['models'])}", "",
        "## Verdict distribution", *[f"- {k}: {v}" for k, v in stats["verdicts"].most_common()], "",
        "## Confidence distribution", *[f"- {k}: {v}" for k, v in sorted(buckets.items())],
        f"- forced to review: {sum(stats['review_reasons'].values())} — reasons: {dict(stats['review_reasons'])}", "",
        "## Fetch / scan", f"- fetch: {dict(stats['fetch'])}", f"- surface-scan findings by check: {dict(stats['scan_by_check'])}", "",
        "## Yield per recipe (qualified = target segment, no review flag)",
        *[f"- {name}: {stats['per_recipe_qualified'][name]}/{n} qualified" for name, n in yield_rows], "",
        "## Broken / degraded", *([f"- {b}" for b in stats["broken"]] or ["- none"]),
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", nargs="*")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    t0 = time.monotonic()
    conn = dbmod.get_connection()
    sids = _select_sessions(conn, a)
    if not sids:
        print("no sessions selected (no BULK:* sessions yet? use --sessions or --all)"); sys.exit(1)
    st = export(conn, sids)
    md = summary_md(conn, sids, st, f"export took {time.monotonic()-t0:.0f}s")
    (OUT_DIR / "run_summary.md").write_text(md, encoding="utf-8")
    conn.close()
    print(md)
    print(f"files -> {OUT_DIR / 'leads_core.csv'}, {OUT_DIR / 'leads_content.csv'}, {OUT_DIR / 'prefilter_rejects.csv'}, {OUT_DIR / 'run_summary.md'}")
