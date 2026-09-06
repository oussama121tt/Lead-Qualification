"""Guards against the prompt-diet regression: the system prompt MUST name
every key the parser reads, and alias drift must be normalized, not dropped."""
import scorer


def test_prompt_names_every_schema_key():
    missing = [k for k in scorer.SCHEMA_KEYS if f'"{k}"' not in scorer.SYSTEM_PROMPT]
    assert not missing, f"SYSTEM_PROMPT no longer names schema keys: {missing}"


def test_prompt_stays_short():
    assert len(scorer.SYSTEM_PROMPT.split()) < 1200


def test_alias_normalization_recovers_model_drift():
    raw = {"segment": "ai_solo_founder", "confidence": 0.9, "offer": "ai_audit",
           "hooks": [{"hook": "h", "based_on": "q"}], "quotes": ["q"]}
    out = scorer._normalize_verdict_keys(dict(raw))
    assert out["recommended_offer"] == "ai_audit"
    assert out["personalization_hooks"][0]["hook"] == "h"
    assert out["evidence_quotes"] == ["q"]
    assert "schema_key_aliases_normalized" in out["disqualify_reason"]


def test_no_alias_no_note():
    out = scorer._normalize_verdict_keys({"segment": "unclear", "confidence": 0.2})
    assert "disqualify_reason" not in out
