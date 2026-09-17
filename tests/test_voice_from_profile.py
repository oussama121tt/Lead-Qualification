"""Commit A: hook sentences and subject bans come from the active profile.

The old hardcoded wording ("code audit" / "an audit" / "audit for") would
have leaked RuyaTech offer language into every profile. These tests fail on
that old code when run under the example profile.
"""
import campaign_fields
import personal_line
import profile as profilemod


def _example():
    profilemod.clear_cache()
    return profilemod.load_profile("example")


def test_personal_line_system_has_no_audit_wording_under_example():
    system = personal_line.build_system(_example()).lower()
    assert "code audit" not in system
    assert "an audit" not in system


def test_campaign_fields_system_has_no_audit_wording_under_example():
    system = campaign_fields.build_system(_example()).lower()
    assert "code audit" not in system
    assert "an audit" not in system


def test_example_systems_use_the_profile_implication():
    p = _example()
    assert "a readiness review" in personal_line.build_system(p)
    assert "a readiness review" in campaign_fields.build_system(p)
    assert "readiness review matter" in personal_line.build_system(p)
    assert "readiness review matter" in campaign_fields.build_system(p)


def test_subject_guard_reads_profile_bans():
    profilemod.clear_cache()
    ruya = profilemod.load_profile("ruyatech")
    assert campaign_fields._subject_banned(ruya).search("can you do an audit for us")
    assert campaign_fields._subject_banned(_example()).search("can you do an audit for us") is None
    # Generic spam patterns still apply under every profile.
    assert campaign_fields._subject_banned(_example()).search("Quick question")
    assert campaign_fields._subject_banned().search("Quick question")


def test_hooks_parsed_from_toml():
    profilemod.clear_cache()
    hooks = profilemod.load_profile("ruyatech").voice.hooks
    assert hooks.mention_ban == "an audit"
    assert hooks.personal_line_implication.startswith("Then give the implication")
    assert hooks.sequence_implication.startswith("then the implication")
