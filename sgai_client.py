"""ScrapeGraphAI (SGAI) credit governor — the monthly cap for the PAID trigger
search lane.

funding_announced and product_hunt run ONE governed web search per due lead
per run, and that search bills to ScrapeGraphAI, not Apollo. This module keeps
SGAI's spend in its OWN table and under its OWN cap
([triggers].sgai_monthly_credit_cap), so the Apollo cap and the SGAI cap can
never throttle each other:
  - team_growth            gates on apollo_client.check_credit_budget (Apollo)
  - funding/product_hunt   gate on sgai_client.check_credit_budget (SGAI)

Spend unit is NOMINAL: 1 credit per governed search run, recorded AFTER the
search executes. This makes the cap a real, counted budget (there is no other
usage recorder for the trigger lane). When real SGAI pricing/quota is known,
the toll in triggers._Ctx.governed_websearch can be re-pointed to the true
per-search cost.

Env: SGAI_API_KEY (required to make live calls via
scraper.search_additional_evidence).
"""
from __future__ import annotations

from datetime import datetime


class SgaiCreditCapReached(RuntimeError):
    def __init__(self, used: int, cap: int, needed: int):
        super().__init__(f"SGAI monthly credit cap: {used}/{cap} used, {needed} more needed")
        self.used, self.cap, self.needed = used, cap, needed


# ---------------------------------------------------------------------------
# Credit governor (monthly), persisted in sgai_usage(month, credits_used)
# ---------------------------------------------------------------------------

def ensure_usage_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sgai_usage ("
        "month TEXT PRIMARY KEY, credits_used INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()


def _this_month() -> str:
    return datetime.now().strftime("%Y-%m")


def credits_used_this_month(conn) -> int:
    row = conn.execute(
        "SELECT credits_used FROM sgai_usage WHERE month = ?", (_this_month(),)
    ).fetchone()
    return row["credits_used"] if row else 0


def record_credits(conn, n: int) -> None:
    if n <= 0:
        return
    conn.execute(
        "INSERT INTO sgai_usage (month, credits_used) VALUES (?, ?) "
        "ON CONFLICT (month) DO UPDATE SET credits_used = sgai_usage.credits_used + ?",
        (_this_month(), n, n),
    )
    conn.commit()


def check_credit_budget(conn, needed: int, cap: int) -> None:
    """Raise SgaiCreditCapReached if using `needed` credits would exceed the
    monthly cap. cap <= 0 disables the check."""
    if cap <= 0:
        return
    used = credits_used_this_month(conn)
    if used + needed > cap:
        raise SgaiCreditCapReached(used, cap, needed)