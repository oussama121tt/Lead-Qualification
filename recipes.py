"""Saved Apollo search recipes with historical yield.

Your validated finding: vertical keyword searches ("health app", "hr platform")
yield 80-100% qualified but tiny pools; generic keywords yield huge pools at
~45% qualified with ~25% competitors. Implication: run MANY narrow searches,
not a few broad ones — so recipes must be first-class, named, versioned, and
carry their own track record so the UI can say "this recipe yields 85%
qualified but 0% replies".

A recipe stores the Apollo search `filters` (passed straight to
apollo_client.search_people) plus running counters updated as batches flow
through: runs, leads_pulled, qualified, enriched, sent, replies.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS apollo_recipes ("
        "id INTEGER PRIMARY KEY, name TEXT, filters TEXT, created_at TEXT, "
        "runs INTEGER NOT NULL DEFAULT 0, "
        "leads_pulled INTEGER NOT NULL DEFAULT 0, "
        "qualified INTEGER NOT NULL DEFAULT 0, "
        "enriched INTEGER NOT NULL DEFAULT 0, "
        "sent INTEGER NOT NULL DEFAULT 0, "
        "replies INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()


def create(conn, name: str, filters: dict) -> int:
    ensure_table(conn)
    row = conn.execute(
        "INSERT INTO apollo_recipes (name, filters, created_at) VALUES (?, ?, ?) RETURNING id",
        (name, json.dumps(filters, ensure_ascii=False), _now()),
    ).fetchone()
    conn.commit()
    return row["id"]


def get(conn, recipe_id: int) -> dict | None:
    ensure_table(conn)
    row = conn.execute("SELECT * FROM apollo_recipes WHERE id = ?", (recipe_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["filters"] = json.loads(d["filters"]) if d.get("filters") else {}
    except (json.JSONDecodeError, TypeError):
        d["filters"] = {}
    d["qualified_rate"] = round(d["qualified"] / d["leads_pulled"], 2) if d["leads_pulled"] else None
    d["reply_rate"] = round(d["replies"] / d["sent"], 3) if d["sent"] else None
    return d


def list_all(conn) -> list[dict]:
    ensure_table(conn)
    rows = conn.execute("SELECT * FROM apollo_recipes ORDER BY id DESC").fetchall()
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["filters"] = json.loads(d["filters"]) if d.get("filters") else {}
        except (json.JSONDecodeError, TypeError):
            d["filters"] = {}
        d["qualified_rate"] = round(d["qualified"] / d["leads_pulled"], 2) if d["leads_pulled"] else None
        d["reply_rate"] = round(d["replies"] / d["sent"], 3) if d["sent"] else None
        out.append(d)
    return out


def record_run(conn, recipe_id: int, *, leads_pulled: int, qualified: int,
               enriched: int) -> None:
    """Update a recipe's counters after a sourcing run (one search + prefilter
    + enrich cycle)."""
    ensure_table(conn)
    conn.execute(
        "UPDATE apollo_recipes SET runs = runs + 1, "
        "leads_pulled = leads_pulled + ?, qualified = qualified + ?, "
        "enriched = enriched + ? WHERE id = ?",
        (leads_pulled, qualified, enriched, recipe_id),
    )
    conn.commit()


def record_outcomes(conn, recipe_id: int, *, sent: int = 0, replies: int = 0) -> None:
    """Update send/reply counters (from the sending tool / Apollo analytics)."""
    ensure_table(conn)
    conn.execute(
        "UPDATE apollo_recipes SET sent = sent + ?, replies = replies + ? WHERE id = ?",
        (sent, replies, recipe_id),
    )
    conn.commit()


def delete(conn, recipe_id: int) -> None:
    ensure_table(conn)
    conn.execute("DELETE FROM apollo_recipes WHERE id = ?", (recipe_id,))
    conn.commit()
