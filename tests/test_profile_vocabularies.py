"""Commit B: stages/sensitive/budget vocabularies come from the profile.

Fallback behavior is unchanged, only its source moved: an out-of-list
value validates to None/[]/sentinel exactly as the old hardcoded sets did.
"""
import scorer
import profile as profilemod


def _v(**over):
    base = {
        "segment": "ai_solo_founder",
        "confidence": 0.85,
        "company_stage": "early",
        "recommended_offer": "ai_audit",
        "sensitive_data_categories": [],
        "data_sensitivity_score": 0,
        "budget_signal": "strong",
        "budget_evidence": [],
        "budget_blockers": [],
        "needs_human_review": False,
    }
    base.update(over)
    return base


def test_stage_out_of_list_validates_to_none():
    v = scorer._validate_verdict(_v(company_stage="series-a"))
    assert v["company_stage"] is None
    assert scorer._validate_verdict(_v(company_stage="scaling"))["company_stage"] == "scaling"


def test_sensitive_out_of_list_is_stripped():
    v = scorer._validate_verdict(_v(sensitive_data_categories=["candidate_pii"]))
    assert v["sensitive_data_categories"] == []
    v = scorer._validate_verdict(_v(sensitive_data_categories=["minors", "unknown", "none"]))
    assert v["sensitive_data_categories"] == ["minors"]


def test_sensitive_sentinel_alone_is_kept():
    v = scorer._validate_verdict(_v(sensitive_data_categories=["none"]))
    assert v["sensitive_data_categories"] == ["none"]
    v = scorer._validate_verdict(_v(sensitive_data_categories=["none", "minors"]))
    assert v["sensitive_data_categories"] == ["minors"]


def test_budget_out_of_list_falls_back_to_sentinel():
    v = scorer._validate_verdict(_v(budget_signal="enterprise"))
    assert v["budget_signal"] == "none"
    assert scorer._validate_verdict(_v(budget_signal="weak"))["budget_signal"] == "weak"


def test_tables_load_from_both_profiles():
    profilemod.clear_cache()
    ruya = profilemod.load_profile("ruyatech")
    assert ruya.stages.values == ["pre-launch", "early", "scaling", "established"]
    assert "minors" in ruya.sensitive.categories
    assert ruya.sensitive.empty_sentinel == "none"
    assert ruya.sensitive.score_max == 100
    assert ruya.budget.signals == ["strong", "moderate", "weak", "none"]
    assert ruya.budget.empty_sentinel == "none"
    profilemod.clear_cache()
    example = profilemod.load_profile("example")
    assert example.stages.values == ["pre-launch", "early", "scaling", "established"]
    assert "customer_data" in example.sensitive.categories
    assert "minors" not in example.sensitive.categories
    profilemod.clear_cache()


def test_example_prompt_uses_example_vocabularies():
    profilemod.clear_cache()
    prompt = scorer.get_system_prompt(profilemod.load_profile("example"))
    assert "customer_data" in prompt
    assert "minors" not in prompt
    assert "customer records" in prompt
    profilemod.clear_cache()
