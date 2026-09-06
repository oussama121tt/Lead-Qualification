"""Stage-0 pre-filter rules — the biggest cost lever. Deterministic, offline."""
from prefilter import evaluate_person, prefilter_people


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
