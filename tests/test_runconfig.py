"""Config loader: live values vs the [fast] test overlay."""
from runconfig import load_config


def test_live_config_has_real_pacing_and_caps():
    cfg = load_config(fast=False)
    assert cfg.linkedin.delay_min >= 30
    assert cfg.linkedin.daily_cap > 0
    assert cfg.linkedin.bypass_caps is False
    assert cfg.website.free_first is True


def test_fast_overlay_shrinks_delays_and_bypasses_caps():
    cfg = load_config(fast=True)
    assert cfg.linkedin.delay_max <= 10
    assert cfg.linkedin.bypass_caps is True
    # Non-overlaid values keep their live settings.
    assert cfg.linkedin.daily_cap > 0
