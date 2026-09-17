"""Task 2 gate: prompts assembled from the ruyatech profile must be
byte-identical to the pre-refactor texts frozen in
tests/fixtures/task2_frozen/ (captured before the move, env cleared)."""
from pathlib import Path

import campaign_fields
import emailer
import personal_line
import profile as profilemod
import scorer

FROZEN = Path(__file__).resolve().parent / "fixtures" / "task2_frozen"

LEAD = {
    "company_name": "Acme Health",
    "first_name": "Jane",
    "segment": "ai_solo_founder",
    "recommended_offer": "ai_audit",
    "personalization_hooks": '[{"hook": "h", "based_on": "q"}]',
    "evidence_quotes": '["exact quote from site"]',
}


def _read(name: str) -> str:
    return (FROZEN / name).read_text(encoding="utf-8")


def test_email_template_byte_identical(monkeypatch):
    monkeypatch.delenv("SENDER_NAME", raising=False)
    monkeypatch.delenv("SENDER_COMPANY", raising=False)
    profilemod.clear_cache()
    p = profilemod.load_profile("ruyatech")
    assert emailer._template_for(p) == _read("email_template.txt")


def test_email_sample_byte_identical(monkeypatch):
    monkeypatch.delenv("SENDER_NAME", raising=False)
    monkeypatch.delenv("SENDER_COMPANY", raising=False)
    profilemod.clear_cache()
    assert emailer.build_prompt(dict(LEAD), "Acme homepage excerpt") == _read("email_sample.txt")


def test_personal_line_system_byte_identical():
    profilemod.clear_cache()
    assert personal_line.build_system(profilemod.load_profile("ruyatech")) == _read(
        "personal_line_system.txt")


def test_campaign_fields_system_byte_identical():
    profilemod.clear_cache()
    assert campaign_fields.build_system(profilemod.load_profile("ruyatech")) == _read(
        "campaign_fields_system.txt")


def test_scorer_system_byte_identical():
    profilemod.clear_cache()
    assert scorer.get_system_prompt(profilemod.load_profile("ruyatech")) == _read(
        "scorer_system.txt")
    assert scorer.SYSTEM_PROMPT == _read("scorer_system.txt")
