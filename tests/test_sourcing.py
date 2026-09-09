"""sourcing.run_recipe: DNC-before-enrich guard, credit governor + cost estimate.

Run offline (no API key, no DB server) by monkeypatching the Apollo HTTP layer
and the db write functions. The key acceptance for this suite: a lead on the
DNC registry is never handed to enrich_people, so no Apollo enrich credit is
spent on it.
"""
import types

import sourcing


def _cfg(max_people_per_run=50, search_page_size=5, monthly_credit_cap=100,
         prefilter_enabled=True):
    return types.SimpleNamespace(
        apollo=types.SimpleNamespace(max_people_per_run=max_people_per_run,
                                     search_page_size=search_page_size,
                                     monthly_credit_cap=monthly_credit_cap),
        prefilter=types.SimpleNamespace(enabled=prefilter_enabled, use_llm=False,
                                        max_headcount=50, min_headcount=0),
    )


def _person(name, domain):
    return {
        "first_name": name,
        "organization": {"primary_domain": domain},
    }


def _prefilter_keep(people):
    return {"keep": people, "reject": [],
            "stats": {"total": len(people), "kept": len(people), "rejected": 0,
                      "unclear_resolved_by_llm": 0}}


class _Sink:
    """Captures enrich calls + db writes so we can assert what run_recipe did."""
    def __init__(self):
        self.enrich_calls = []
        self.inserted_leads = []
        self.sessions = 0
        self.recorded_run = None
        self.flag_import = 0

    def enrich(self, conn, people, *, monthly_cap, reveal_personal_emails=False):
        self.enrich_calls.append(people)
        return {"enriched": people, "credits": len(people)}

    def create_session(self, conn, **kw):
        self.sessions += 1
        return self.sessions

    def insert(self, conn, lead_rows, batch_id, session_id=None):
        self.inserted_leads = lead_rows
        return {"inserted": len(lead_rows)}

    def flag(self, conn, session_id):
        self.flag_import += 1

    def record_run(self, conn, recipe_id, **kw):
        self.recorded_run = kw


def _install(monkeypatch, sink):
    monkeypatch.setattr(sourcing, "load_config", lambda: _cfg())
    monkeypatch.setattr(sourcing.prefiltermod, "prefilter_people",
                        lambda people, **k: _prefilter_keep(people))
    monkeypatch.setattr(sourcing.apollo_client, "enrich_people", sink.enrich)
    monkeypatch.setattr(sourcing.dbmod, "create_analysis_session", sink.create_session)
    monkeypatch.setattr(sourcing.dbmod, "insert_leads_from_rows", sink.insert)
    monkeypatch.setattr(sourcing.dncmod, "flag_batch_on_import", sink.flag)
    monkeypatch.setattr(sourcing.recipesmod, "record_run", sink.record_run)
    monkeypatch.setattr(sourcing.apollo_client, "ensure_usage_table", lambda conn: None)
    monkeypatch.setattr(sourcing.apollo_client, "credits_used_this_month", lambda conn: 0)
    monkeypatch.setattr(sourcing.recipesmod, "get", lambda conn, rid: {"filters": {"q": "x"}})


# --- Part 2 acceptance: DNC'd lead never reaches enrich (no credit spent) ---

def test_dnc_lead_is_never_enriched(monkeypatch):
    """A lead on the DNC (by domain) is dropped before enrich_people is called —
    so zero enrich credits are spent on it. This is the DNC-before-enrich guard."""
    sink = _Sink()
    _install(monkeypatch, sink)
    keep = [_person("Keep It", "good.com"), _person("Dnc Me", "blocked.com")]
    monkeypatch.setattr(sourcing.apollo_client, "search_people_all", lambda *a, **k: keep)
    monkeypatch.setattr(sourcing.dncmod, "load_sets",
                        lambda conn: (set(), {"blocked.com"}))
    # recipes.get is patched in _install, but pass explicit filters to skip lookup.
    summary = sourcing.run_recipe(None, filters={"q": "x"})

    # Only one person survives DNC and reaches enrich.
    assert sink.enrich_calls == [[_person("Keep It", "good.com")]]
    assert summary["dnc_skipped_before_enrich"] == 1
    assert summary["to_enrich"] == 1
    assert summary["enriched"] == 1
    assert summary["credits_spent"] == 1  # only the non-DNC lead cost a credit


def test_all_dnc_leads_cost_zero_credits(monkeypatch):
    """If every survivor is on the DNC, the pipeline short-circuits before the
    credit-gated enrich step — no enrich call, no credit charged."""
    sink = _Sink()
    _install(monkeypatch, sink)
    keep = [_person("A", "blocked.com"), _person("B", "blocked.com")]
    monkeypatch.setattr(sourcing.apollo_client, "search_people_all", lambda *a, **k: keep)
    monkeypatch.setattr(sourcing.dncmod, "load_sets",
                        lambda conn: (set(), {"blocked.com"}))
    summary = sourcing.run_recipe(None, filters={"q": "x"})

    assert sink.enrich_calls == []
    assert summary["to_enrich"] == 0
    assert summary["credits_spent"] == 0
    assert summary["enriched"] == 0


# --- Credit governor: hard monthly cap + per-run cost estimate before spend ---

def test_over_budget_raises_before_any_enrich(monkeypatch):
    """The ApolloCreditCapReached guard fires before the enrich HTTP call when
    the run's needed credits would exceed the monthly cap — so hard budget is
    enforced and no spend happens."""
    import apollo_client
    sink = _Sink()
    _install(monkeypatch, sink)
    keep = [_person("A", "good.com"), _person("B", "good.com"), _person("C", "good.com")]
    monkeypatch.setattr(sourcing.apollo_client, "search_people_all", lambda *a, **k: keep)
    monkeypatch.setattr(sourcing.dncmod, "load_sets", lambda conn: (set(), set()))
    monkeypatch.setattr(sourcing.apollo_client, "check_credit_budget",
                        lambda conn, needed, cap: (_ for _ in ()).throw(
                            apollo_client.ApolloCreditCapReached(used=2, cap=2, needed=needed)))
    monkeypatch.setattr(sourcing, "load_config", lambda: _cfg(monthly_credit_cap=2))

    try:
        sourcing.run_recipe(None, filters={"q": "x"})
        assert False, "expected ApolloCreditCapReached"
    except apollo_client.ApolloCreditCapReached:
        pass
    assert sink.enrich_calls == []  # never spent anything


def test_cost_estimate_returned_before_spend_on_dry_run(monkeypatch):
    """dry_run reports credits_needed + current usage before spending anything —
    the per-run cost estimate is surfaced to the caller ahead of any spend."""
    sink = _Sink()
    _install(monkeypatch, sink)
    keep = [_person("A", "good.com"), _person("B", "good.com")]
    monkeypatch.setattr(sourcing.apollo_client, "search_people_all", lambda *a, **k: keep)
    monkeypatch.setattr(sourcing.dncmod, "load_sets", lambda conn: (set(), set()))
    monkeypatch.setattr(sourcing.apollo_client, "credits_used_this_month", lambda conn: 37)
    summary = sourcing.run_recipe(None, filters={"q": "x"}, dry_run=True)

    assert summary["dry_run"] is True
    assert summary["to_enrich"] == 2
    assert summary["credits_needed"] == 2
    assert summary["credits_used_this_month"] == 37
    assert summary["monthly_cap"] == 100
    assert sink.enrich_calls == []  # dry run: zero credits spent
