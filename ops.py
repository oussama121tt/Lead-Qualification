"""Operations dashboard data (/ops): what is running, what it costs, what is
left on every external budget. One function, `snapshot(conn)`, returns a plain
dict the template renders; every external call is isolated and cached so a
slow or dead vendor never breaks the page.

Sources:
  - leads / lead_scores / analysis_sessions : pipeline state and throughput
  - llm_calls                                : LLM spend by provider/model (today, month)
  - apollo_usage + Apollo credit_usage_stats : credits used here vs the account balance
  - sgai_usage  + ScrapeGraphAI /credits     : per-key remaining credits (14-key ring)
  - provider_status                          : last Anthropic rate-limit headers (written by llm_provider)
  - do_not_contact, export_history, lead_outcomes, lead_trigger_events, campaigns
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

_CACHE: dict[str, tuple[float, object]] = {}
_LOCK = threading.Lock()
EXTERNAL_TTL_S = 60


def _cached(key: str, ttl: float, fn):
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        val = fn()
    except Exception as exc:  # the dashboard must render whatever else works
        val = {"error": f"{exc.__class__.__name__}: {str(exc)[:160]}"}
    with _LOCK:
        _CACHE[key] = (now, val)
    return val


def _q(conn, sql, params=()):
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []


def _one(conn, sql, params=(), default=0):
    rows = _q(conn, sql, params)
    if not rows:
        return default
    v = next(iter(rows[0].values()))
    return v if v is not None else default


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------- external

def apollo_credits() -> dict:
    key = os.getenv("APOLLO_API_KEY", "").strip()
    if not key:
        return {"error": "APOLLO_API_KEY not set"}
    r = requests.post("https://api.apollo.io/api/v1/usage_stats/credit_usage_stats",
                      headers={"x-api-key": key}, timeout=15)
    r.raise_for_status()
    stats = r.json().get("credit_usage_stats") or {}
    lead = stats.get("lead_credit") or {}
    return {"limit": lead.get("limit"), "consumed": lead.get("consumed"),
            "left": lead.get("left_over"), "raw_keys": sorted(stats.keys())}


def sgai_credits() -> dict:
    keys = []
    for name, val in sorted(os.environ.items(), key=lambda kv: (len(kv[0]), kv[0])):
        if re.fullmatch(r"SGAI_API_KEY_?\d*", name) and val and val.strip():
            keys.append(val.strip())
    multi = os.getenv("SCRAPE_API_KEYS", "").strip()
    if multi:
        keys += [k.strip() for k in multi.split(",") if k.strip()]
    seen, ordered = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            ordered.append(k)
    def one(i_k):
        i, k = i_k
        try:
            r = requests.get("https://v2-api.scrapegraphai.com/api/credits",
                             headers={"SGAI-APIKEY": k}, timeout=10)
            if r.status_code == 200:
                j = r.json()
                return {"n": i, "tail": k[-4:], "remaining": int(j.get("remaining") or 0),
                        "used": j.get("used"), "plan": j.get("plan")}
            return {"n": i, "tail": k[-4:], "remaining": None, "error": f"HTTP {r.status_code}"}
        except Exception as exc:
            return {"n": i, "tail": k[-4:], "remaining": None, "error": exc.__class__.__name__}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        per_key = list(pool.map(one, list(enumerate(ordered, 1))))
    total_left = sum(k["remaining"] or 0 for k in per_key)
    return {"per_key": per_key, "total_remaining": total_left, "n_keys": len(ordered)}


# ---------------------------------------------------------------- snapshot

def snapshot(conn, *, external: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    h1 = _iso(now - timedelta(hours=1))
    m5 = _iso(now - timedelta(minutes=5))
    day = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")

    # -- pipeline state ----------------------------------------------------
    by_status = {r["status"]: r["n"] for r in _q(
        conn, "SELECT status, COUNT(*) AS n FROM leads WHERE is_duplicate = 0 GROUP BY status")}
    dup = _one(conn, "SELECT COUNT(*) AS n FROM leads WHERE is_duplicate = 1")
    total = _one(conn, "SELECT COUNT(*) AS n FROM leads")
    pending_statuses = ("NEW", "PARSED", "FETCH_PARTIAL", "FETCH_FAILED",
                        "SCORE_FAILED", "RESCORE_PENDING", "RESCORE_FAILED")
    pending = sum(by_status.get(s, 0) for s in pending_statuses)
    scored_5m = _one(conn, "SELECT COUNT(*) AS n FROM lead_scores WHERE scored_at >= ?", (m5,))
    scored_1h = _one(conn, "SELECT COUNT(*) AS n FROM lead_scores WHERE scored_at >= ?", (h1,))
    last = _q(conn, "SELECT l.company_name, s.segment, s.confidence, s.scored_at "
                    "FROM lead_scores s JOIN leads l ON l.id = s.lead_id "
                    "ORDER BY s.id DESC LIMIT 8")
    running_sessions = _q(conn, "SELECT id, label, status, created_at FROM analysis_sessions "
                                "WHERE status = 'running' ORDER BY id DESC")
    active = scored_5m > 0
    rate_per_min = round(scored_5m / 5.0, 2)
    eta_min = round(pending / rate_per_min) if (active and rate_per_min) else None

    # -- verdict quality (latest verdict per lead) ------------------------
    seg = {r["segment"]: r["n"] for r in _q(conn, """
        SELECT s.segment, COUNT(*) AS n FROM lead_scores s
        JOIN (SELECT lead_id, MAX(id) AS mid FROM lead_scores GROUP BY lead_id) m ON m.mid = s.id
        JOIN leads l ON l.id = s.lead_id WHERE l.is_duplicate = 0 GROUP BY s.segment""")}
    fp = {r["founder_profile"] or "unknown": r["n"] for r in _q(conn, """
        SELECT s.founder_profile, COUNT(*) AS n FROM lead_scores s
        JOIN (SELECT lead_id, MAX(id) AS mid FROM lead_scores GROUP BY lead_id) m ON m.mid = s.id
        JOIN leads l ON l.id = s.lead_id WHERE l.is_duplicate = 0 GROUP BY s.founder_profile""")}
    be = {r["build_evidence"] or "unknown": r["n"] for r in _q(conn, """
        SELECT s.build_evidence, COUNT(*) AS n FROM lead_scores s
        JOIN (SELECT lead_id, MAX(id) AS mid FROM lead_scores GROUP BY lead_id) m ON m.mid = s.id
        JOIN leads l ON l.id = s.lead_id WHERE l.is_duplicate = 0 GROUP BY s.build_evidence""")}
    review = _one(conn, """
        SELECT COUNT(*) AS n FROM lead_scores s
        JOIN (SELECT lead_id, MAX(id) AS mid FROM lead_scores GROUP BY lead_id) m ON m.mid = s.id
        JOIN leads l ON l.id = s.lead_id WHERE l.is_duplicate = 0 AND s.needs_human_review = 1""")
    grounding_losses_1h = _one(conn, """
        SELECT COUNT(*) AS n FROM lead_scores WHERE scored_at >= ?
          AND disqualify_reason LIKE '%ungrounded_evidence_quotes_removed%'""", (h1,))

    # -- LLM spend -------------------------------------------------------------
    llm_today = _q(conn, """
        SELECT provider, model, COUNT(*) AS calls, COALESCE(SUM(tokens_in),0) AS tokens_in,
               COALESCE(SUM(tokens_out),0) AS tokens_out, COALESCE(SUM(cost_usd),0) AS usd
        FROM llm_calls WHERE created_at >= ? GROUP BY provider, model ORDER BY usd DESC""", (day,))
    llm_month = _q(conn, """
        SELECT provider, model, COUNT(*) AS calls, COALESCE(SUM(cost_usd),0) AS usd
        FROM llm_calls WHERE created_at >= ? GROUP BY provider, model ORDER BY usd DESC""", (month + "-01",))
    llm_1h = _one(conn, "SELECT COUNT(*) AS n FROM llm_calls WHERE created_at >= ?", (h1,))
    for r in llm_today + llm_month:
        r["usd"] = round(float(r["usd"] or 0), 4)

    # -- vendor budgets ----------------------------------------------------------
    apollo_used_here = _one(conn, "SELECT credits_used AS n FROM apollo_usage WHERE month = ?", (month,))
    sgai_used_here = _one(conn, "SELECT credits_used AS n FROM sgai_usage WHERE month = ?", (month,))
    provider_status = {r["provider"]: json.loads(r["payload"]) if r.get("payload") else {}
                       for r in _q(conn, "SELECT provider, payload, updated_at FROM provider_status")}
    for r in _q(conn, "SELECT provider, updated_at FROM provider_status"):
        provider_status.setdefault(r["provider"], {})["updated_at"] = r["updated_at"]

    ext = {}
    if external:
        ext["apollo"] = _cached("apollo_credits", EXTERNAL_TTL_S, apollo_credits)
        ext["sgai"] = _cached("sgai_credits", EXTERNAL_TTL_S, sgai_credits)

    # -- outreach state ----------------------------------------------------------
    outreach = {
        "dnc": _one(conn, "SELECT COUNT(*) AS n FROM do_not_contact"),
        "exported": _one(conn, "SELECT COUNT(DISTINCT lead_id) AS n FROM export_history"),
        "approved": _one(conn, "SELECT COUNT(*) AS n FROM leads WHERE review_status = 'APPROVED'"),
        "rejected": _one(conn, "SELECT COUNT(*) AS n FROM leads WHERE review_status = 'REJECTED'"),
        "drafted": _one(conn, "SELECT COUNT(*) AS n FROM leads WHERE email_body IS NOT NULL AND email_body <> ''"),
        "enrolled": _one(conn, "SELECT COUNT(*) AS n FROM leads WHERE email_status = 'enrolled_apollo'"),
        "outcomes": _q(conn, "SELECT COUNT(*) AS n, COALESCE(SUM(opened),0) AS opened, "
                             "COALESCE(SUM(replied),0) AS replied FROM lead_outcomes"),
        "campaigns": _q(conn, "SELECT id, name, status, session_id FROM campaigns ORDER BY id DESC LIMIT 5"),
        "trigger_events": _one(conn, "SELECT COUNT(*) AS n FROM lead_trigger_events"),
    }

    return {
        "generated_at": _iso(now),
        "pipeline": {
            "total": total, "duplicates": dup, "by_status": by_status, "pending": pending,
            "scored_5m": scored_5m, "scored_1h": scored_1h, "rate_per_min": rate_per_min,
            "active": active, "eta_min": eta_min, "last": last,
            "running_sessions": running_sessions,
        },
        "quality": {"segment": seg, "founder_profile": fp, "build_evidence": be,
                    "needs_review": review, "grounding_losses_1h": grounding_losses_1h},
        "llm": {"today": llm_today, "month": llm_month, "calls_1h": llm_1h,
                "today_usd": round(sum(r["usd"] for r in llm_today), 4),
                "month_usd": round(sum(r["usd"] for r in llm_month), 4),
                "provider_status": provider_status},
        "vendors": {"apollo_used_here": apollo_used_here, "sgai_used_here": sgai_used_here, **ext},
        "outreach": outreach,
    }


def ensure_status_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS provider_status ("
                 "provider TEXT PRIMARY KEY, payload TEXT, updated_at TEXT)")
    conn.commit()


def record_provider_status(conn, provider: str, payload: dict) -> None:
    """Upsert the latest vendor headers (rate limits etc.). Called by
    llm_provider after a call, throttled by the caller."""
    ensure_status_table(conn)
    now = _iso(datetime.now(timezone.utc))
    conn.execute(
        "INSERT INTO provider_status (provider, payload, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (provider) DO UPDATE SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at",
        (provider, json.dumps(payload, ensure_ascii=False), now),
    )
    conn.commit()
