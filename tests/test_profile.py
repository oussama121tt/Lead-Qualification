"""Task 1: profile loader returns typed dataclasses, cached per process."""
import profile as profilemod


def test_load_ruyatech_identity():
    profilemod.clear_cache()
    p = profilemod.load_profile("ruyatech")
    assert p.identity.company == "RuyaTech"
    assert isinstance(p.identity.proof_points, list) and p.identity.proof_points
    assert p.segments["ai_solo_founder"].offer == "ai_audit"
    assert "ai_solo_founder" in p.target_segments


def test_profile_cached_per_process():
    profilemod.clear_cache()
    assert profilemod.load_profile("ruyatech") is profilemod.load_profile("ruyatech")


def test_env_selects_profile(monkeypatch):
    profilemod.clear_cache()
    monkeypatch.setenv("LEAD_PROFILE", "ruyatech")
    assert profilemod.load_profile().identity.company == "RuyaTech"
    profilemod.clear_cache()
