"""Stage-0 pre-filter rules — the biggest cost lever. Deterministic, offline."""
from prefilter import _to_int, evaluate_person, prefilter_people


def test_rejects_agency_company():
    v = evaluate_person({"title": "Founder", "company_name": "Bright Digital Agency"})
    assert v["decision"] == "reject"


def test_rejects_fractional_cto_title():
    v = evaluate_person({"title": "Fractional CTO", "company_name": "SomeCo"})
    assert v["decision"] == "reject"


def test_rejects_consultancy():
    v = evaluate_person({"title": "Owner", "company_name": "Acme Consulting"})
    assert v["decision"] == "reject"


def test_rejects_too_big_by_headcount():
    v = evaluate_person({"title": "Founder", "company_name": "BigCo",
                         "estimated_num_employees": 5000}, max_headcount=50)
    assert v["decision"] == "reject"


def test_rejects_non_decision_maker():
    v = evaluate_person({"title": "Sales Development Representative", "company_name": "Startup"})
    assert v["decision"] == "reject"


def test_keeps_clear_founder():
    v = evaluate_person({"title": "Co-Founder & CEO", "company_name": "HealthApp",
                         "estimated_num_employees": 4})
    assert v["decision"] == "keep"


def test_founder_title_beats_recruiter_word():
    # "Founder" present → keep even if another word looks non-decision.
    v = evaluate_person({"title": "Founder (also handles recruiting)", "company_name": "Tiny"})
    assert v["decision"] == "keep"


def test_unclear_kept_when_llm_disabled():
    v = evaluate_person({"title": "Head of Product", "company_name": "Nimbus"})
    assert v["decision"] == "unclear"
    out = prefilter_people([{"title": "Head of Product", "company_name": "Nimbus"}], use_llm=False)
    assert out["stats"]["kept"] == 1   # unclear → kept, never rejected on ambiguity


def test_prefilter_batch_split_and_stats():
    people = [
        {"title": "Founder", "company_name": "HealthApp", "estimated_num_employees": 3},
        {"title": "CEO", "company_name": "Dev Shop Agency"},
        {"title": "Recruiter", "company_name": "BigCorp"},
    ]
    out = prefilter_people(people, use_llm=False)
    assert out["stats"]["total"] == 3
    assert out["stats"]["kept"] == 1
    assert out["stats"]["rejected"] == 2
    assert len(out["keep"]) == 1


# --- _to_int: Apollo headcount formats --------------------------------------

RANGE_CASES = [
    ("11-50", 11),
    ("1-10", 1),
    ("51-200", 51),
    ("201-500", 201),
    ("501-1000", 501),
    ("1001-5000", 1001),
    ("5000+", 5000),
    ("11,50", 11),          # Apollo filter-style comma range
    ("250,1000", 250),
    ("1,001-5,000", 1001),  # thousands grouping with dash range
    ("1,001", 1001),        # thousands-grouped single number, not a range
    (" 50 ", 50),
    ("11 to 50", 11),
]
SINGLE_CASES = [
    (5, 5),
    (50.0, 50),
    ("50", 50),
    ("1200", 1200),
    ("10001+", 10001),
]
NONE_CASES = [None, "", "  ", "N/A", "small", "n/a"]


def test_to_int_parses_apollo_ranges_and_singletons():
    for raw, expected in RANGE_CASES + SINGLE_CASES:
        assert _to_int(raw) == expected, f"expected {raw!r} -> {expected}"


def test_to_int_returns_none_for_empty_or_garbage():
    for raw in NONE_CASES:
        assert _to_int(raw) is None, f"expected {raw!r} -> None"


def test_range_headcount_no_longer_false_rejects():
    """Regression: "11-50" used to parse as 1150 and get rejected as too big.
    A target-band company must be kept."""
    v = evaluate_person({"title": "Founder", "company_name": "Acme",
                         "estimated_num_employees": "11-50"}, max_headcount=50)
    assert v["decision"] == "keep"


def test_range_headcount_still_rejects_over_target():
    v = evaluate_person({"title": "Founder", "company_name": "Acme",
                         "estimated_num_employees": "51-200"}, max_headcount=50)
    assert v["decision"] == "reject"


# --- Stage-0 cost logging (FR-7) -------------------------------------------

class _FakeProvider:
    name = "groq"
    model = "llama-3.3-70b-versatile"

    def generate_json(self, prompt, *, system=None, temperature=None, max_tokens=1024):
        return ({"decision": "keep", "reason": "ok"},
                {"provider": self.name, "model": self.model,
                 "tokens_in": 100, "tokens_out": 20})


def test_stage0_llm_spend_is_costlogged(monkeypatch):
    """Stage-0's Groq pass must write a llm_calls row with purpose='prefilter'
    (FR-7), same as scoring/email stages — and only when the LLM is used."""
    import sqlite3
    import costlog
    import pipeline
    import llm_provider
    import prefilter
    monkeypatch.setattr(llm_provider, "get_llm_provider", lambda purpose="email": _FakeProvider())

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    costlog.ensure_table(conn)

    unclear = {"title": "Head of Product", "company_name": "Nimbus"}
    cost_cb = pipeline._make_cost_cb(conn, None, None, "prefilter")
    out = prefilter.prefilter_people([unclear], use_llm=True, cost_cb=cost_cb)
    assert out["stats"]["unclear_resolved_by_llm"] == 1

    row = conn.execute("SELECT * FROM llm_calls").fetchone()
    assert row is not None
    assert row["purpose"] == "prefilter"
    assert row["provider"] == "groq"
    assert row["tokens_in"] == 100 and row["tokens_out"] == 20
    assert row["cost_usd"] is not None and row["cost_usd"] > 0
    assert row["session_id"] is None and row["lead_id"] is None


def test_stage0_no_cost_logged_when_llm_disabled(monkeypatch):
    import sqlite3
    import costlog
    import llm_provider
    import prefilter
    monkeypatch.setattr(llm_provider, "get_llm_provider", lambda purpose="email": _FakeProvider())

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    costlog.ensure_table(conn)

    prefilter.prefilter_people([{"title": "Head of Product", "company_name": "Nimbus"}],
                               use_llm=False, cost_cb=lambda *a, **k: None)
    assert conn.execute("SELECT COUNT(*) AS c FROM llm_calls").fetchone()["c"] == 0


def test_run_recipe_threads_cost_cb_into_prefilter(monkeypatch):
    """sourcing.run_recipe must hand prefilter_people a real cost_cb when the
    Stage-0 LLM is enabled (that is the actual FR-7 wiring)."""
    import types
    import sourcing
    calls = {}

    def fake_prefilter(people, *, max_headcount, min_headcount, use_llm, cost_cb):
        calls["cost_cb"] = cost_cb
        calls["use_llm"] = use_llm
        return {"keep": [], "reject": [], "stats": {"total": 0, "kept": 0, "rejected": 0, "unclear_resolved_by_llm": 0}}

    cfg = types.SimpleNamespace(
        apollo=types.SimpleNamespace(max_people_per_run=10, search_page_size=5, monthly_credit_cap=100),
        prefilter=types.SimpleNamespace(enabled=True, use_llm=True, max_headcount=50, min_headcount=0),
    )
    monkeypatch.setattr(sourcing, "load_config", lambda: cfg)
    monkeypatch.setattr(sourcing.prefiltermod, "prefilter_people", fake_prefilter)
    monkeypatch.setattr(sourcing.apollo_client, "search_people_all", lambda *a, **k: [])
    monkeypatch.setattr(sourcing.dncmod, "load_sets", lambda conn: (set(), set()))
    monkeypatch.setattr(sourcing.apollo_client, "ensure_usage_table", lambda conn: None)
    monkeypatch.setattr(sourcing.apollo_client, "credits_used_this_month", lambda conn: 0)

    sourcing.run_recipe(None, filters={"q": "founder"}, dry_run=True)
    assert calls["use_llm"] is True
    assert callable(calls["cost_cb"])  # → every Stage-0 LLM call hits llm_calls
    # Without the LLM enabled, no cost callback should be created.
    calls.clear()
    cfg.prefilter.use_llm = False
    sourcing.run_recipe(None, filters={"q": "founder"}, dry_run=True)
    assert calls["cost_cb"] is None
