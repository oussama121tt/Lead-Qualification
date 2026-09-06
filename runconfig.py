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
    require_verified_email: bool


@dataclass
class PrefilterCfg:
    enabled: bool
    use_llm: bool
    max_headcount: int
    min_headcount: int


@dataclass
class Config:
    fast: bool
    linkedin: LinkedInCfg
    website: WebsiteCfg
    budget: BudgetCfg
    surface_scan: SurfaceScanCfg
    apollo: ApolloCfg
    prefilter: PrefilterCfg


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
            require_verified_email=bool(raw.get("apollo", {}).get("require_verified_email", True)),
        ),
        prefilter=PrefilterCfg(
            enabled=bool(raw.get("prefilter", {}).get("enabled", True)),
            use_llm=bool(raw.get("prefilter", {}).get("use_llm", False)),
            max_headcount=int(raw.get("prefilter", {}).get("max_headcount", 50)),
            min_headcount=int(raw.get("prefilter", {}).get("min_headcount", 0)),
        ),
    )
    if path is None:
        _cached = cfg
    return cfg
