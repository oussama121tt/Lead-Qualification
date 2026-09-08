"""Typed config loader for config.toml. Ported from lead_tool's config.py.

Usage:
    cfg = load_config()             # live values
    cfg = load_config(fast=True)    # [fast] overlay (test mode)
    cfg.linkedin.delay_min          # -> 2 in fast mode, 45 otherwise

Set RUN_MODE=fast in the environment to select fast mode app-wide without
touching code (picked up by load_config's default).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.toml"


@dataclass
class LinkedInCfg:
    delay_min: float
    delay_max: float
    long_pause_every_min: int
    long_pause_every_max: int
    long_pause_min: float
    long_pause_max: float
    daily_cap: int
    weekly_cap: int
    post_interval: float
    authored_keep: int
    liked_keep: int
    max_posts: int
    bypass_caps: bool


@dataclass
class WebsiteCfg:
    page_timeout: float
    per_domain_delay: float
    free_first: bool


@dataclass
class BudgetCfg:
    session_cap_usd: float


@dataclass
class SurfaceScanCfg:
    enabled: bool
    timeout: float
    per_domain_delay: float
    max_findings: int


@dataclass
class ApolloCfg:
    monthly_credit_cap: int
    search_page_size: int
    max_people_per_run: int


@dataclass
class PrefilterCfg:
    enabled: bool
    use_llm: bool
    max_headcount: int
    min_headcount: int


@dataclass
class TriggersCfg:
    enabled: bool
    interval_days: int
    max_leads_per_run: int
    high_value_confidence: float
    team_growth_delta: int
    reorder_queue: bool
    sgai_monthly_credit_cap: int


@dataclass
class MailboxCfg:
    name: str
    daily_cap: int
    # Warmup ramp (optional): start at ramp_start_cap on ramp_start (ISO date)
    # and rise linearly to daily_cap over ramp_days. A mailbox with no ramp is
    # steady-state at full daily_cap.
    ramp_start: str | None = None
    ramp_days: int = 0
    ramp_start_cap: int = 0


@dataclass
class CostCfg:
    apollo_credit_price_usd: float
    scrape_price_usd: float
    operator_rate_usd_hour: float
    review_minutes_per_lead: float


@dataclass
class SendingCfg:
    """Outbound-email capacity config for the send-capacity planner.

    The live Gmail sender is still single-account today; these mailboxes model
    the (future) multi-mailbox fleet and drive the capacity planner's forecast.
    """
    mailboxes: list[MailboxCfg]
    # Touch offsets in days from a contact's add date at which each follow-up
    # goes out. len() is the touch count. Default = 3 touches (T1@0, T2@3, T3@7).
    sequence_offsets: tuple[int, ...] = (0, 3, 7)


@dataclass
class Config:
    fast: bool
    linkedin: LinkedInCfg
    website: WebsiteCfg
    budget: BudgetCfg
    surface_scan: SurfaceScanCfg
    apollo: ApolloCfg
    prefilter: PrefilterCfg
    triggers: TriggersCfg
    sending: SendingCfg
    costs: CostCfg


_cached: Config | None = None


def load_config(fast: bool | None = None, path: Path | None = None) -> Config:
    """Loads config.toml. fast=None reads RUN_MODE from the environment.
    The result is cached per process (config is not meant to change mid-run)."""
    global _cached
    if fast is None:
        fast = os.getenv("RUN_MODE", "").strip().lower() == "fast"
    if _cached is not None and _cached.fast == fast and path is None:
        return _cached

    raw = tomllib.loads((path or CONFIG_PATH).read_text(encoding="utf-8"))
    li = dict(raw["linkedin"])
    fast_over = raw.get("fast", {}) if fast else {}
    li.update({k: v for k, v in fast_over.items() if k in li})
    bypass_caps = bool(fast_over.get("bypass_caps", False))

    cfg = Config(
        fast=fast,
        linkedin=LinkedInCfg(
            delay_min=li["delay_min"],
            delay_max=li["delay_max"],
            long_pause_every_min=li["long_pause_every_min"],
            long_pause_every_max=li["long_pause_every_max"],
            long_pause_min=li["long_pause_min"],
            long_pause_max=li["long_pause_max"],
            daily_cap=li["daily_cap"],
            weekly_cap=li["weekly_cap"],
            post_interval=li["post_interval"],
            authored_keep=li["authored_keep"],
            liked_keep=li["liked_keep"],
            max_posts=li["max_posts"],
            bypass_caps=bypass_caps,
        ),
        website=WebsiteCfg(
            page_timeout=raw["website"]["page_timeout"],
            per_domain_delay=raw["website"]["per_domain_delay"],
            free_first=bool(raw["website"].get("free_first", True)),
        ),
        budget=BudgetCfg(
            session_cap_usd=float(raw.get("budget", {}).get("session_cap_usd", 0.0)),
        ),
        surface_scan=SurfaceScanCfg(
            enabled=bool(raw.get("surface_scan", {}).get("enabled", False)),
            timeout=float(raw.get("surface_scan", {}).get("timeout", 10)),
            per_domain_delay=float(raw.get("surface_scan", {}).get("per_domain_delay", 1.0)),
            max_findings=int(raw.get("surface_scan", {}).get("max_findings", 8)),
        ),
        apollo=ApolloCfg(
            monthly_credit_cap=int(raw.get("apollo", {}).get("monthly_credit_cap", 0)),
            search_page_size=int(raw.get("apollo", {}).get("search_page_size", 100)),
            max_people_per_run=int(raw.get("apollo", {}).get("max_people_per_run", 500)),
        ),
        prefilter=PrefilterCfg(
            enabled=bool(raw.get("prefilter", {}).get("enabled", True)),
            use_llm=bool(raw.get("prefilter", {}).get("use_llm", False)),
            max_headcount=int(raw.get("prefilter", {}).get("max_headcount", 50)),
            min_headcount=int(raw.get("prefilter", {}).get("min_headcount", 0)),
        ),
        triggers=TriggersCfg(
            enabled=bool(raw.get("triggers", {}).get("enabled", False)),
            interval_days=int(raw.get("triggers", {}).get("interval_days", 7)),
            max_leads_per_run=int(raw.get("triggers", {}).get("max_leads_per_run", 50)),
            high_value_confidence=float(raw.get("triggers", {}).get("high_value_confidence", 0.8)),
            team_growth_delta=int(raw.get("triggers", {}).get("team_growth_delta", 3)),
            reorder_queue=bool(raw.get("triggers", {}).get("reorder_queue", True)),
            # SGAI owns the PAID trigger search lane (funding_announced /
            # product_hunt) with its own cap, independent of Apollo. Placeholder
            # budget (1 nominal credit per governed search run); 0 = off.
            sgai_monthly_credit_cap=int(raw.get("triggers", {}).get("sgai_monthly_credit_cap", 1000)),
        ),
        sending=_load_sending(raw),
        costs=CostCfg(
            apollo_credit_price_usd=float(raw.get("costs", {}).get("apollo_credit_price_usd", 0.0)),
            scrape_price_usd=float(raw.get("costs", {}).get("scrape_price_usd", 0.0)),
            operator_rate_usd_hour=float(raw.get("costs", {}).get("operator_rate_usd_hour", 0.0)),
            review_minutes_per_lead=float(raw.get("costs", {}).get("review_minutes_per_lead", 0.0)),
        ),
    )
    if path is None:
        _cached = cfg
    return cfg


def _load_sending(raw: dict) -> SendingCfg:
    send = raw.get("sending", {})
    mb_list = []
    for m in send.get("mailboxes", []):
        mb_list.append(MailboxCfg(
            name=str(m.get("name", "")),
            daily_cap=int(m.get("daily_cap", 0)),
            ramp_start=(str(m["ramp_start"]) if m.get("ramp_start") else None),
            ramp_days=int(m.get("ramp_days", 0)),
            ramp_start_cap=int(m.get("ramp_start_cap", 0)),
        ))
    offsets_raw = send.get("sequence_offsets", [0, 3, 7])
    offsets = tuple(int(o) for o in offsets_raw)
    return SendingCfg(mailboxes=mb_list, sequence_offsets=offsets)
