"""Real criteria filtering: a deselected segment is excluded from the prompt
and rejected in validation; a custom criterion is a mandatory gate."""
import sqlite3
from contextlib import contextmanager

import scorer
import profile as profilemod


def _ruyatech():
    profilemod.clear_cache()
    return profilemod.load_profile("ruyatech")


def test_system_prompt_lists_only_checked_segments():
    p = _ruyatech()
    prompt = scorer.get_system_prompt(p, scoring_criteria=["ai_solo_founder"])
    for seg in ("technical_founder", "small_agency_scaling", "too_big", "wrong_field", "unclear"):
        assert seg not in prompt
    assert "ai_solo_founder" in prompt
    assert "ai_audit" in prompt
    assert "general_audit" not in prompt
    assert "pipeline" not in prompt
    profilemod.clear_cache()


def test_system_prompt_unfiltered_by_default():
    p = _ruyatech()
    assert scorer.get_system_prompt(p) == scorer.get_system_prompt(p, scoring_criteria=None)
    assert scorer.get_system_prompt(p) == scorer.get_system_prompt(p, scoring_criteria=[])
    profilemod.clear_cache()


def _verdict(**over):
    base = {
        "segment": "technical_founder", "confidence": 0.85,
        "founder_profile": "technical", "build_evidence": "hand_built",
        "company_stage": "scaling", "recommended_offer": "general_audit",
        "needs_human_review": False,
    }
    base.update(over)
    return base


def test_deselected_segment_reclassified():
    v = scorer._validate_verdict(_verdict(), scoring_criteria=["ai_solo_founder"])
    assert v["segment"] == "unclear"
    assert v["confidence"] == 0.3
    assert v["needs_human_review"] is True
    assert "deselected_segment_fixed_to_unclear:technical_founder" in (v["disqualify_reason"] or "")


def test_allowed_segment_passes_through():
    v = scorer._validate_verdict(
        _verdict(segment="ai_solo_founder", recommended_offer="ai_audit",
                 founder_profile="non_technical", build_evidence="ai_built"),
        scoring_criteria=["ai_solo_founder"])
    assert v["segment"] == "ai_solo_founder"
    assert v["confidence"] == 0.85
    assert v["needs_human_review"] is False


def test_custom_criterion_is_mandatory_gate(monkeypatch):
    captured = {}
    canned = {
        "segment": "ai_solo_founder", "confidence": 0.86,
        "founder_profile": "non_technical", "build_evidence": "ai_built",
        "company_stage": "early", "recommended_offer": "ai_audit",
        "built_with_ai_signals": ["lovable"], "technical_signals": [],
        "pain_signals": [], "evidence_quotes": ["Acme builds widgets with AI tools for small teams."],
        "personalization_hooks": [], "sensitive_data_categories": [],
        "data_sensitivity_score": 0, "budget_signal": "none",
        "budget_evidence": [], "budget_blockers": [],
        "disqualify_reason": None, "needs_human_review": False,
    }

    def fake_call(user_content, max_output_tokens=scorer.MAX_OUTPUT_TOKENS, **kwargs):
        captured["content"] = user_content
        return dict(canned)

    monkeypatch.setattr(scorer, "_call_llm", fake_call)
    rows = [("homepage", "https://acme.example", "Acme builds widgets with AI tools for small teams.")]
    scorer.score_content(rows, scoring_criteria=["ai_solo_founder"],
                         scoring_criteria_custom="uses Python and Django")
    content = captured["content"]
    assert "MUST" in content
    assert "uses Python and Django" in content
    assert "give more weight" not in content


def _client(monkeypatch):
    import app as appmod

    @contextmanager
    def _open():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        yield conn

    monkeypatch.setattr(appmod, "open_db", _open)
    monkeypatch.setattr(appmod, "_init_done", True)
    monkeypatch.setattr(appmod.dbmod, "get_analysis_session",
                         lambda c, sid: {"id": sid, "label": f"sess-{sid}", "owner_id": None})
    monkeypatch.setattr(appmod.dbmod, "get_user_by_id",
                         lambda c, uid: {"id": uid, "email": "t@t.com", "role": "admin", "is_active": True})
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = 1
        sess["role"] = "admin"
    return c


def test_start_refuses_with_no_criteria(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/import/1/start", data={})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/import/1")


def test_start_refuses_with_lenses_only(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/import/1/start", data={"criteria": "solo_or_small"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/import/1")
