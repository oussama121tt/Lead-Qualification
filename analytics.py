"""Phase 4 — Analytics queries (Tasks 16–18).

Task 16  Signal→Outcome attribution: per-signal Leads / Sent / Replies /
         Reply-rate / Lift, lift computed against the overall baseline.
Task 17  Cost per outcome: $/reply and $/close split by recipe / segment /
         channel, covering Apollo credits + LLM (llm_calls.cost_usd) +
         scraping + operator time.
Task 18  Channel comparison: cold email vs upwork vs discord vs inbound on
         one table (leads, cost, replies, closes, revenue, cycle length).

Signal naming drift is reconciled HERE as a derived view (no migration):
  * the scraper writes wide fingerprint columns on lead_technical_signals
    (app_builder_fingerprint, site_builder_fingerprint, on_builder_subdomain);
  * the scorer stores JSON lists on lead_scores (technical_signals,
    pain_signals, sensitive_data_categories) plus budget_signal/segment;
  * trigger monitor writes relational rows on lead_trigger_events(trigger).
All three fold into one canonical (lead, family, value) row-set.

Everything is plain SQL + sqlite-compatible, so the module is unit-testable
offline (test harness builds its own schema; no PG = no ensure_table calls).
"""
from __future__ import annotations

import json
from datetime import date

CHANNELS = ("cold_email", "upwork", "discord", "inbound")

# Minimum number of SENT leads a signal must have before we report a lift vs
# the baseline. Below this, lift is None (UI shows "not enough data") — a raw
# rate over 1-2 sends is noise, e.g. 1 send/1 reply = 10x baseline but proves
# nothing. Keep it as a module constant so /analytics passes it straight to the
# template; promote to [costs] in config.toml when the operator needs to tune it.
MIN_SENT_FOR_LIFT = 10


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _flag(row: dict, col: str) -> int:
    v = row.get(col)
    try:
        return 1 if int(v) else 0
    except (TypeError, ValueError):
        return 0


def _is_sent(row: dict) -> bool:
    return bool(row.get("outcome_sent_at") or row.get("email_sent_at"))


def _replied(row: dict) -> bool:
    return _flag(row, "replied") == 1


def _closed(row: dict) -> bool:
    return _flag(row, "closed_won") == 1


def _fnum(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _json_list(value) -> list[str]:
    """Parses a possibly-JSON Text column into a list of strings, tolerating
    bare strings, already-lists, and None."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    s = str(value).strip()
    if not s:
        return []
    if s[:1] in "[{":
        try:
            parsed = json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return [s]
        if isinstance(parsed, list):
            return [str(v) for v in parsed if v]
        if isinstance(parsed, dict):
            return [str(v) for v in parsed.values() if v]
    return [s]


# ---------------------------------------------------------------------------
# Derived lead-row view (all signal sources + outcomes + channel)
# ---------------------------------------------------------------------------

def _load_leads(conn, session_id=None) -> list[dict]:
    """One row per lead with every signal source, the outcome row and the
    session channel. Latest score/signals row wins (same MAX(id) idiom as
    db.get_leads_with_scores)."""
    query = """
        SELECT l.id AS lead_id, l.session_id, l.email, l.email_sent_at,
               l.review_status,
               s.segment, s.budget_signal, s.technical_signals, s.pain_signals,
               s.sensitive_data_categories,
               t.app_builder_fingerprint, t.site_builder_fingerprint,
               t.on_builder_subdomain,
               o.sent_at AS outcome_sent_at, o.opened, o.clicked, o.replied,
               o.reply_sentiment, o.meeting_booked, o.closed_won, o.revenue,
               a.created_at AS session_created_at,
               COALESCE(a.channel, 'cold_email') AS channel
        FROM leads l
        LEFT JOIN lead_scores s ON s.lead_id = l.id
            AND s.id = (SELECT MAX(id) FROM lead_scores WHERE lead_id = l.id)
        LEFT JOIN lead_technical_signals t ON t.lead_id = l.id
            AND t.id = (SELECT MAX(id) FROM lead_technical_signals WHERE lead_id = l.id)
        LEFT JOIN lead_outcomes o ON o.lead_id = l.id
        LEFT JOIN analysis_sessions a ON a.id = l.session_id
    """
    params = []
    if session_id is not None:
        query += " WHERE l.session_id = ?"
        params.append(session_id)
    query += " ORDER BY l.id"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def _load_triggers(conn, session_id=None) -> dict[int, list[str]]:
    """lead_id -> list of trigger names (deduped)."""
    out: dict[int, list[str]] = {}
    query = "SELECT lead_id, trigger FROM lead_trigger_events"
    params = []
    if session_id is not None:
        query += " WHERE session_id = ?"
        params.append(session_id)
    for r in conn.execute(query, params):
        t = str(r["trigger"]).strip()
        if not t:
            continue
        out.setdefault(r["lead_id"], [])
        if t not in out[r["lead_id"]]:
            out[r["lead_id"]].append(t)
    return out


def signal_families(row: dict) -> list[tuple[str, str]]:
    """Canonical (family, value) pairs for one derived lead row. Handles BOTH
    signal shapes: the scorer's JSON lists and the scraper's wide columns."""
    fams: list[tuple[str, str]] = []
    seg = row.get("segment")
    if seg:
        fams.append(("segment", str(seg)))
    budget = row.get("budget_signal")
    if budget and str(budget) != "none":
        fams.append(("budget", str(budget)))
    for v in _json_list(row.get("sensitive_data_categories")):
        if v != "none":
            fams.append(("sensitive_data", v))
    for v in _json_list(row.get("pain_signals")):
        fams.append(("pain", v))
    for v in _json_list(row.get("technical_signals")):
        fams.append(("technical", v))
    app_builder = row.get("app_builder_fingerprint")
    if app_builder:
        fams.append(("app_builder", str(app_builder)))
    site_builder = row.get("site_builder_fingerprint")
    if site_builder:
        fams.append(("site_builder", str(site_builder)))
    if row.get("on_builder_subdomain"):
        fams.append(("on_builder_subdomain", "yes"))
    return fams


def signal_rows(conn, session_id=None) -> list[dict]:
    """Flat (lead, family, value) rows with outcome flags — the raw material
    for attribution."""
    rows: list[dict] = []
    leads = _load_leads(conn, session_id=session_id)
    triggers = _load_triggers(conn, session_id=session_id)
    for r in leads:
        lfs = set(signal_families(r))
        for t in triggers.get(r["lead_id"], []):
            lfs.add(("trigger", t))
        for fam, val in lfs:
            rows.append({
                "lead_id": r["lead_id"],
                "channel": r.get("channel") or "cold_email",
                "family": fam,
                "value": val,
                "sent": int(_is_sent(r)),
                "replied": int(_replied(r)),
                "opened": _flag(r, "opened"),
            })
    return rows


# ---------------------------------------------------------------------------
# Task 16 — Signal → Outcome attribution
# ---------------------------------------------------------------------------

def attribution(conn, session_id=None) -> tuple[list[dict], dict]:
    """Per-signal Leads / Sent / Replies / Reply-rate / Lift.

    Leads  = leads carrying the signal; Sent = leads of those that were sent
    to; Replies = sent leads that replied; Lift = that signal's reply rate
    divided by the overall baseline reply rate (>1 → beats the baseline).
    Lift is only computed when the signal has >= MIN_SENT_FOR_LIFT sends;
    below that it's None (the UI shows "not enough data")."""
    leads = _load_leads(conn, session_id=session_id)
    triggers = _load_triggers(conn, session_id=session_id)

    stats: dict[tuple[str, str], dict[str, set]] = {}
    for r in leads:
        lfs = set(signal_families(r))
        for t in triggers.get(r["lead_id"], []):
            lfs.add(("trigger", t))
        for fam, val in lfs:
            g = stats.setdefault((fam, val), {"leads": set(), "sent": set(), "replied": set()})
            g["leads"].add(r["lead_id"])
            if _is_sent(r):
                g["sent"].add(r["lead_id"])
            if _replied(r):
                g["replied"].add(r["lead_id"])

    baseline_sent = {r["lead_id"] for r in leads if _is_sent(r)}
    baseline_replied = {r["lead_id"] for r in leads if _replied(r)}
    baseline_rate = (len(baseline_replied) / len(baseline_sent)) if baseline_sent else None

    rows = []
    for (fam, val), g in stats.items():
        rate = (len(g["replied"]) / len(g["sent"])) if g["sent"] else None
        enough = len(g["sent"]) >= MIN_SENT_FOR_LIFT
        lift = (rate / baseline_rate) if (rate is not None and baseline_rate and enough) else None
        rows.append({
            "family": fam,
            "value": val,
            "leads": len(g["leads"]),
            "sent": len(g["sent"]),
            "replies": len(g["replied"]),
            "reply_rate": round(rate, 4) if rate is not None else None,
            "lift": round(lift, 2) if lift is not None else None,
        })
    rows.sort(key=lambda r: (r["sent"], r["replies"]), reverse=True)

    baseline = {
        "leads": len(leads),
        "sent": len(baseline_sent),
        "replies": len(baseline_replied),
        "reply_rate": round(baseline_rate, 4) if baseline_rate is not None else None,
    }
    return rows, baseline


def last_sync_summary(conn) -> dict | None:
    """Latest Apollo analytics sync month -> messages/opened/clicked/replied."""
    try:
        row = conn.execute(
            "SELECT month, MAX(fetched_at) AS fetched_at, COUNT(*) AS messages, "
            "SUM(opened) AS opened, SUM(clicked) AS clicked, SUM(replied) AS replied "
            "FROM apollo_analytics_sync_report GROUP BY month ORDER BY month DESC LIMIT 1"
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Cost ledgers (Task 17 supports)
# ---------------------------------------------------------------------------

def _cost_ledgers(conn) -> tuple[dict[int, float], dict[int, int]]:
    """(llm_cost_by_lead, scraped_pages_by_lead) from llm_calls + lead_content."""
    llm: dict[int, float] = {}
    try:
        for r in conn.execute(
            "SELECT lead_id, SUM(cost_usd) AS cost FROM llm_calls WHERE lead_id IS NOT NULL GROUP BY lead_id"
        ):
            llm[r["lead_id"]] = _fnum(r["cost"])
    except Exception:
        pass
    scraped: dict[int, int] = {}
    try:
        for r in conn.execute(
            "SELECT lead_id, COUNT(*) AS n FROM lead_content WHERE lead_id IS NOT NULL GROUP BY lead_id"
        ):
            scraped[r["lead_id"]] = int(r["n"])
    except Exception:
        pass
    return llm, scraped


def _recipe_by_session(conn) -> dict[int, int]:
    try:
        return {
            r["session_id"]: r["recipe_id"]
            for r in conn.execute(
                "SELECT session_id, recipe_id FROM campaigns WHERE session_id IS NOT NULL AND recipe_id IS NOT NULL"
            )
        }
    except Exception:
        return {}


def _recipes(conn) -> dict[int, dict]:
    try:
        return {
            int(r["id"]): dict(r)
            for r in conn.execute("SELECT id, name, enriched FROM apollo_recipes")
        }
    except Exception:
        return {}


def _apollo_usage_total(conn) -> int:
    """Total Apollo credits used (apollo_usage ledger). Shared by
    cost_per_outcome and channel_comparison so both apportion the same spend."""
    try:
        row = conn.execute("SELECT COALESCE(SUM(credits_used), 0) AS n FROM apollo_usage").fetchone()
        return int(row["n"] if row else 0)
    except Exception:
        return 0


def _lead_cost(row: dict, llm: dict, scraped: dict, cfg) -> float:
    c = _fnum(llm.get(row["lead_id"]))
    if cfg is not None:
        c += (scraped.get(row["lead_id"]) or 0) * _fnum(cfg.scrape_price_usd)
        if row.get("review_status"):
            c += (_fnum(cfg.review_minutes_per_lead) / 60.0) * _fnum(cfg.operator_rate_usd_hour)
    return round(c, 4)


def cost_per_outcome(conn, cfg=None) -> dict:
    """$/reply and $/close per recipe / segment / channel.

    Recipe → Apollo credit spend is exact-ish (recipe.enriched × credit price).
    Segment/channel → Apollo spend is apportioned by lead-count share of the
    total credits used (the search/enrich reports don't tag a lead's segment).
    Scraping + operator time are estimates (config [costs])."""
    price = _fnum(cfg.apollo_credit_price_usd) if cfg is not None else 0.0
    leads = _load_leads(conn)
    llm, scraped = _cost_ledgers(conn)
    recipe_by_session = _recipe_by_session(conn)
    recipes = _recipes(conn)

    groups: dict[str, dict] = {k: {} for k in ("recipe", "segment", "channel")}

    def _bucket(kind: str, key, label: str, lead: dict) -> dict:
        g = groups[kind].setdefault(key, {
            "key": key, "label": label, "leads": 0, "sent": 0, "replies": 0,
            "closed_won": 0, "revenue": 0.0, "cost": 0.0,
        })
        g["leads"] += 1
        if _is_sent(lead):
            g["sent"] += 1
        if _replied(lead):
            g["replies"] += 1
        if _closed(lead):
            g["closed_won"] += 1
        g["revenue"] += _fnum(lead.get("revenue"))
        g["cost"] += _lead_cost(lead, llm, scraped, cfg)
        return g

    for lead in leads:
        lid, sid = lead["lead_id"], lead.get("session_id")
        seg = (lead.get("segment") or "").strip() or "unsegmented"
        ch = (lead.get("channel") or "").strip() or "cold_email"
        rid = recipe_by_session.get(sid)
        if rid is None:
            _bucket("recipe", "no_recipe", "No recipe (upload)", lead)
        else:
            label = recipes.get(rid, {}).get("name") or f"Recipe #{rid}"
            _bucket("recipe", rid, label, lead)

        _bucket("segment", seg, seg, lead)
        _bucket("channel", ch, ch, lead)

    # Apollo credit spend: per-recipe exact (enriched count), segment/channel
    # apportioned by lead-count share.
    for rid, info in recipes.items():
        g = groups["recipe"].get(rid)
        if g is None:
            continue
        g["cost"] += _fnum(info.get("enriched")) * price
    total_credits = _apollo_usage_total(conn)
    total_apollo = total_credits * price
    if total_apollo:
        for kind in ("segment", "channel"):
            total_leads = sum(g["leads"] for g in groups[kind].values())
            if not total_leads:
                continue
            for g in groups[kind].values():
                g["cost"] += round(total_apollo * (g["leads"] / total_leads), 4)

    def _finalize(kind: str, totals: dict) -> list[dict]:
        rows = []
        for g in groups[kind].values():
            g["cost"] = round(g["cost"], 4)
            g["reply_rate"] = round(g["replies"] / g["sent"], 4) if g["sent"] else None
            g["cost_per_reply"] = round(g["cost"] / g["replies"], 4) if g["replies"] else None
            g["cost_per_close"] = round(g["cost"] / g["closed_won"], 4) if g["closed_won"] else None
            g["revenue"] = round(g["revenue"], 2)
            g["net_revenue"] = round(g["revenue"] - g["cost"], 2)
            rows.append(g)
            for k in ("cost", "revenue", "replies", "closed_won", "sent", "leads", "net_revenue"):
                totals[k] = totals.get(k, 0) + (g.get(k) or 0)
        rows.sort(key=lambda r: r["sent"], reverse=True)
        return rows

    totals = {"leads": 0, "sent": 0, "replies": 0, "closed_won": 0, "cost": 0.0, "revenue": 0.0}
    recipes_rows = _finalize("recipe", totals)
    segments_rows = _finalize("segment", dict.fromkeys(totals, 0.0))
    channels_rows = _finalize("channel", dict.fromkeys(totals, 0.0))
    return {
        "recipes": recipes_rows,
        "segments": segments_rows,
        "channels": channels_rows,
        "totals": totals,
        "assumptions": {
            "apollo_credit_price_usd": price,
            "scrape_price_usd": _fnum(cfg.scrape_price_usd) if cfg is not None else 0.0,
            "operator_rate_usd_hour": _fnum(cfg.operator_rate_usd_hour) if cfg is not None else 0.0,
            "review_minutes_per_lead": _fnum(cfg.review_minutes_per_lead) if cfg is not None else 0.0,
        },
    }


# ---------------------------------------------------------------------------
# Task 18 — Channel comparison
# ---------------------------------------------------------------------------

def _cycle_days(row: dict) -> float | None:
    """Whole days from session creation to first send (negative → 0)."""
    sent = row.get("outcome_sent_at") or row.get("email_sent_at")
    created = row.get("session_created_at")
    if not sent or not created:
        return None
    try:
        s = date.fromisoformat(str(sent)[:10])
        c = date.fromisoformat(str(created)[:10])
    except (ValueError, TypeError):
        return None
    return max(0.0, float((s - c).days))


def channel_comparison(conn, cfg=None) -> list[dict]:
    """One comparable row per channel: Leads / Sent / Cost / Replies / Closes /
    Revenue / $/reply / reply-rate / cycle length (avg days to first send)."""
    leads = _load_leads(conn)
    llm, scraped = _cost_ledgers(conn)

    per: dict[str, dict] = {}
    for r in leads:
        ch = (r.get("channel") or "").strip() or "cold_email"
        g = per.setdefault(ch, {
            "channel": ch, "leads": 0, "sent": 0, "replies": 0, "closed_won": 0,
            "revenue": 0.0, "cost": 0.0, "cycle_days_total": 0.0, "cycle_n": 0,
        })
        g["leads"] += 1
        if _is_sent(r):
            g["sent"] += 1
        if _replied(r):
            g["replies"] += 1
        if _closed(r):
            g["closed_won"] += 1
        g["revenue"] += _fnum(r.get("revenue"))
        g["cost"] += _lead_cost(r, llm, scraped, cfg)
        d = _cycle_days(r)
        if d is not None:
            g["cycle_days_total"] += d
            g["cycle_n"] += 1

    # Apollo credit spend apportioned by lead-count share of total credits used
    # — the SAME rule as cost_per_outcome's channel split, so /analytics/channels
    # and /analytics/costs show the same cost for the same channel.
    price = _fnum(cfg.apollo_credit_price_usd) if cfg is not None else 0.0
    total_apollo = _apollo_usage_total(conn) * price
    if total_apollo:
        total_leads = sum(g["leads"] for g in per.values())
        if total_leads:
            for g in per.values():
                g["cost"] += round(total_apollo * (g["leads"] / total_leads), 4)

    rows = []
    for g in per.values():
        g["cost"] = round(g["cost"], 4)
        g["revenue"] = round(g["revenue"], 2)
        g["net_revenue"] = round(g["revenue"] - g["cost"], 2)
        g["reply_rate"] = round(g["replies"] / g["sent"], 4) if g["sent"] else None
        g["cost_per_reply"] = round(g["cost"] / g["replies"], 4) if g["replies"] else None
        g["cost_per_close"] = round(g["cost"] / g["closed_won"], 4) if g["closed_won"] else None
        g["cycle_days"] = round(g["cycle_days_total"] / g["cycle_n"], 1) if g["cycle_n"] else None
        g.pop("cycle_days_total", None)
        g.pop("cycle_n", None)
        rows.append(g)
    rows.sort(key=lambda r: r["sent"] or 0, reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Channel assignment UI support
# ---------------------------------------------------------------------------

def session_channels(conn, owner_id=None) -> list[dict]:
    """Recent sessions + their channel (for the per-session assignment UI)."""
    query = ("SELECT id, label, created_at, COALESCE(channel, 'cold_email') AS channel "
             "FROM analysis_sessions")
    params: list = []
    if owner_id is not None:
        query += " WHERE owner_id = ?"
        params.append(owner_id)
    query += " ORDER BY id DESC LIMIT 200"
    return [dict(r) for r in conn.execute(query, params).fetchall()]