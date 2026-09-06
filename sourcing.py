"""Apollo sourcing orchestrator — the "no human sourcing" pipeline.

One run =  search (free) -> Stage-0 pre-filter (free) -> credit-gated enrich
(1 credit per survivor) -> insert enriched leads into a new analysis session,
with the DNC registry applied on import.

Order is enforced here and never violated:
  1. search_people_all           FREE
  2. prefilter_people            FREE  (drops obvious non-fits)
  3. dedup against DNC on the KEEP set (avoid spending credits on people we
     must not contact)                FREE
  4. check_credit_budget + enrich_people   COSTS 1 credit per survivor
  5. insert enriched -> leads, flag DNC/dupes on import
  6. record recipe yield

Returns a summary dict for the UI / logs.
"""
from __future__ import annotations

import uuid

import apollo_client
import db as dbmod
import dnc as dncmod
import prefilter as prefiltermod
import recipes as recipesmod
from runconfig import load_config


def run_recipe(conn, *, recipe_id: int | None = None, filters: dict | None = None,
               owner_id: int | None = None, label: str | None = None,
               dry_run: bool = False) -> dict:
    """Execute one sourcing run.

    Either recipe_id (loads stored filters + updates its yield) or filters
    (ad-hoc) must be given. dry_run stops after the pre-filter and reports
    what WOULD be enriched, spending zero credits.
    """
    cfg = load_config()
    recipe = None
    if recipe_id is not None:
        recipe = recipesmod.get(conn, recipe_id)
        if recipe is None:
            raise ValueError(f"recipe {recipe_id} not found")
        filters = recipe["filters"]
    if not filters:
        raise ValueError("no filters provided")
    # Verified-email gate at SEARCH time (free): never enrich a contact Apollo
    # already knows has no usable email.
    if cfg.apollo.require_verified_email and "contact_email_status" not in filters:
        filters = {**filters, "contact_email_status": ["verified"]}

    # 1. SEARCH (free)
    people = apollo_client.search_people_all(
        filters,
        max_people=cfg.apollo.max_people_per_run,
        per_page=cfg.apollo.search_page_size,
    )
    pulled = len(people)

    # 2. PRE-FILTER (free) — Stage-0
    pf = prefiltermod.prefilter_people(
        people,
        max_headcount=cfg.prefilter.max_headcount,
        min_headcount=cfg.prefilter.min_headcount,
        use_llm=cfg.prefilter.use_llm,
    ) if cfg.prefilter.enabled else {"keep": people, "reject": [], "stats": {"total": pulled, "kept": pulled, "rejected": 0, "unclear_resolved_by_llm": 0}}
    survivors = pf["keep"]

    # 3. DNC dedup on the KEEP set (never spend a credit on a must-not-contact)
    dnc_emails, dnc_domains = dncmod.load_sets(conn)
    pre_dnc_survivors = []
    dnc_skipped = 0
    for p in survivors:
        org = p.get("organization") or {}
        domain = org.get("primary_domain") or p.get("organization_domain")
        # Search-level rows have obfuscated email; DNC at this point is domain-only.
        if dncmod.check_lead(None, domain, dnc_emails, dnc_domains):
            dnc_skipped += 1
            continue
        pre_dnc_survivors.append(p)
    survivors = pre_dnc_survivors

    summary = {
        "pulled": pulled,
        "prefilter": pf["stats"],
        "dnc_skipped_before_enrich": dnc_skipped,
        "to_enrich": len(survivors),
        "credits_needed": len(survivors),
        "enriched": 0,
        "credits_spent": 0,
        "inserted": 0,
        "session_id": None,
        "dry_run": dry_run,
    }

    if dry_run or not survivors:
        # Show the credit position without spending anything.
        apollo_client.ensure_usage_table(conn)
        summary["credits_used_this_month"] = apollo_client.credits_used_this_month(conn)
        summary["monthly_cap"] = cfg.apollo.monthly_credit_cap
        return summary

    # 4. CREDIT-GATED ENRICH (costs credits) — raises ApolloCreditCapReached if over cap
    apollo_client.check_credit_budget(conn, len(survivors), cfg.apollo.monthly_credit_cap)
    enrich_result = apollo_client.enrich_people(
        conn, survivors, monthly_cap=cfg.apollo.monthly_credit_cap
    )
    enriched = enrich_result["enriched"]
    summary["enriched"] = len(enriched)
    summary["credits_spent"] = enrich_result["credits"]

    # 5. INSERT enriched leads into a new session
    lead_rows = [apollo_client.person_to_lead_row(p) for p in enriched]
    lead_rows = [r for r in lead_rows if r.get("website_url")]  # ingester needs a website
    session_id = dbmod.create_analysis_session(
        conn,
        label=label or (recipe["name"] if recipe else "Apollo sourcing"),
        source_filename="apollo_api",
        owner_id=owner_id,
    )
    batch_id = f"apollo_{uuid.uuid4().hex[:8]}"
    ins = dbmod.insert_leads_from_rows(conn, lead_rows, batch_id, session_id=session_id)
    summary["inserted"] = ins["inserted"]
    summary["session_id"] = session_id

    # DNC on import (email-level now that we have real emails) + record export
    # of nothing yet — just flag anyone already on the registry.
    dncmod.flag_batch_on_import(conn, session_id)

    # 6. RECORD recipe yield (qualified = survived prefilter; enriched = credits spent)
    if recipe_id is not None:
        recipesmod.record_run(
            conn, recipe_id,
            leads_pulled=pulled,
            qualified=pf["stats"]["kept"],
            enriched=summary["enriched"],
        )

    apollo_client.ensure_usage_table(conn)
    summary["credits_used_this_month"] = apollo_client.credits_used_this_month(conn)
    summary["monthly_cap"] = cfg.apollo.monthly_credit_cap
    return summary
