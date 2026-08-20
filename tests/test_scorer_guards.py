"""Scorer safety guards — pure functions, no network/API needed."""
import scorer


def _verdict(**over):
    v = {
        "segment": "ai_solo_founder",
        "confidence": 0.9,
        "company_stage": "early",
        "built_with_ai_signals": [],
        "technical_signals": [],
        "pain_signals": [],
        "evidence_quotes": [],
        "recommended_offer": "ai_audit",
        "personalization_hooks": [],
        "disqualify_reason": None,
        "needs_human_review": False,
    }
    v.update(over)
    return v


def test_invalid_segment_forced_to_unclear_and_confidence_capped():
    v = scorer._validate_verdict(_verdict(segment="vibe_coder", confidence=0.95))
    assert v["segment"] == "unclear"
    assert v["needs_human_review"] is True
    assert v["confidence"] <= scorer.INVALID_VERDICT_CONFIDENCE_CAP


def test_valid_verdict_untouched():
    v = scorer._validate_verdict(_verdict())
    assert v["segment"] == "ai_solo_founder"
    assert v["confidence"] == 0.9
    assert v["needs_human_review"] is False


def test_low_confidence_forces_review():
    v = scorer._apply_confidence_guard(_verdict(confidence=0.5))
    assert v["needs_human_review"] is True


def test_ungrounded_evidence_quote_removed_and_flagged():
    source = "We build robust software for ambitious founders."
    v = _verdict(evidence_quotes=["We build robust software", "totally invented quote"])
    out = scorer._verify_evidence_grounding(v, source, None)
    assert out["evidence_quotes"] == ["We build robust software"]
    assert out["needs_human_review"] is True


def test_hook_without_verbatim_citation_discarded():
    source = "Careers: we are hiring three backend engineers this quarter."
    v = _verdict(personalization_hooks=[
        {"hook": "you're hiring three backend engineers",
         "based_on": "we are hiring three backend engineers"},
        {"hook": "your product is collapsing",
         "based_on": "the product is collapsing under load"},  # not in source
    ])
    out = scorer._verify_hooks_grounding(v, source, None)
    kept = out["personalization_hooks"]
    assert len(kept) == 1
    assert kept[0]["based_on"].startswith("we are hiring")


def test_site_missing_guard_forces_review_and_traces_reason():
    v = scorer._apply_site_missing_guard(_verdict(), True)
    assert v["needs_human_review"] is True
    assert "site_content_missing" in (v["disqualify_reason"] or "")
