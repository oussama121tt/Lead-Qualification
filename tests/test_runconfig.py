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


def test_sending_config_loaded():
    """The sending-capacity config (mailboxes + sequence touch offsets) parses
    from config.toml. Default sequence is 3 touches (T1/T2/T3)."""
    cfg = load_config(fast=False)
    assert cfg.sending.mailboxes  # at least one mailbox
    for m in cfg.sending.mailboxes:
        assert m.daily_cap > 0
        assert isinstance(m.name, str) and m.name
    # Default sequence: 3 touches at 0/3/7 days.
    assert cfg.sending.sequence_offsets == (0, 3, 7)
    assert len(cfg.sending.sequence_offsets) == 3
