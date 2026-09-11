"""Two-axis verdict (founder_profile x build_evidence) and the derivation rule.

Why: 381 of 506 live leads collapsed to `unclear` because the single-segment
schema required BOTH "who is the founder" and "how was it built" to be known.
Employment history settles the founder question on its own; the code must not
discard that certainty because the build method is unknown.
"""
import scorer
from runconfig import load_config


def _v(**over):
    base = {
        "segment": "unclear", "confidence": 0.3, "company_stage": "early",
        "recommended_offer": "none", "sensitive_data_categories": [],
        "needs_human_review": True,
    }
    base.update(over)
    return base


def test_schema_and_prompt_carry_both_axes():
    assert "founder_profile" in scorer.SCHEMA_KEYS and "build_evidence" in scorer.SCHEMA_KEYS
    assert '"founder_profile": "non_technical | semi_technical | technical | unknown"' in scorer.SYSTEM_PROMPT
    assert '"build_evidence": "ai_built | hand_built | unknown"' in scorer.SYSTEM_PROMPT
    # 3(a): employment history is sufficient on its own.
    assert "employment history is" in scorer.SYSTEM_PROMPT
    # 3(b): sensitive data requires evidenced handling, not topical adjacency.
    assert "Topical" in scorer.SYSTEM_PROMPT and "HANDLE" in scorer.SYSTEM_PROMPT


def test_invalid_axis_values_become_unknown_without_forced_correction():
    v = scorer._validate_verdict(_v(segment="too_big", confidence=0.95, needs_human_review=False,
                                    founder_profile="wizard", build_evidence=None))
    assert v["founder_profile"] == "unknown" and v["build_evidence"] == "unknown"
    assert v["segment"] == "too_big" and v["confidence"] == 0.95   # untouched


def test_non_technical_founder_with_unknown_build_is_not_unclear():
    v = scorer._validate_verdict(_v(founder_profile="non_technical", build_evidence="unknown", confidence=0.3))
    assert v["segment"] == "ai_solo_founder"
    assert v["recommended_offer"] == "ai_audit"
    assert 0.5 <= v["confidence"] <= 0.7          # honest band, routed to a human
    assert v["needs_human_review"] is True
    assert "segment_derived_from_founder_profile:non_technical" in v["disqualify_reason"]


def test_technical_founder_with_unknown_build_becomes_technical_founder():
    v = scorer._validate_verdict(_v(founder_profile="technical", confidence=0.9))
    assert v["segment"] == "technical_founder" and v["recommended_offer"] == "general_audit"
    assert v["confidence"] == 0.7                  # capped: build still unknown


def test_unknown_founder_stays_unclear_and_reviewed():
    v = scorer._validate_verdict(_v(founder_profile="unknown", build_evidence="unknown"))
    assert v["segment"] == "unclear" and v["needs_human_review"] is True


def test_model_given_segment_is_never_overridden_when_not_unclear():
    v = scorer._validate_verdict(_v(segment="small_agency_scaling", recommended_offer="pipeline",
                                    confidence=0.8, needs_human_review=False,
                                    founder_profile="non_technical", build_evidence="ai_built"))
    assert v["segment"] == "small_agency_scaling" and v["confidence"] == 0.8


def test_apollo_sequences_config_maps_offers():
    cfg = load_config()
    seq = cfg.apollo.sequences
    assert seq is not None and seq.enabled is False          # enrolment is opt-in
    assert seq.sequence_for("ai_audit") == seq.ai_audit
    assert seq.sequence_for("ai_audit", sensitive=True) == seq.ai_audit_sensitive
    assert seq.sequence_for("general_audit") == seq.general_audit
    assert seq.sequence_for("pipeline") is None               # not built yet -> never sent
    assert seq.sequence_for("none") is None


def test_ai_built_with_unknown_founder_is_ai_solo_founder_and_keeps_model_confidence():
    v = scorer._validate_verdict(_v(founder_profile="unknown", build_evidence="ai_built", confidence=0.85,
                                    needs_human_review=False))
    assert v["segment"] == "ai_solo_founder" and v["recommended_offer"] == "ai_audit"
    assert v["confidence"] == 0.85 and v["needs_human_review"] is False
    v2 = scorer._validate_verdict(_v(founder_profile="unknown", build_evidence="ai_built", confidence=0.6))
    assert v2["segment"] == "ai_solo_founder" and v2["needs_human_review"] is True


def test_technical_founder_beats_ai_built_when_both_present():
    v = scorer._validate_verdict(_v(founder_profile="technical", build_evidence="ai_built", confidence=0.9))
    assert v["segment"] == "technical_founder"


def test_budget_blocker_forces_review_and_caps_signal():
    v = scorer._validate_verdict(_v(segment="ai_solo_founder", recommended_offer="ai_audit", confidence=0.85,
                                    needs_human_review=False, budget_signal="strong",
                                    budget_blockers=["student founder"]))
    assert v["needs_human_review"] is True
    assert v["budget_signal"] == "weak"
    clean = scorer._validate_verdict(_v(segment="ai_solo_founder", recommended_offer="ai_audit", confidence=0.85,
                                        needs_human_review=False, budget_signal="strong", budget_blockers=[]))
    assert clean["needs_human_review"] is False and clean["budget_signal"] == "strong"
