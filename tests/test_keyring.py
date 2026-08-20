"""Key rotation: round-robin, cooldown recovery, exhaustion signal."""
from keyring import MemoryKeyRing, QUOTA_COOL, fingerprint


def test_round_robin_spreads_keys():
    ring = MemoryKeyRing(["aaa111", "bbb222", "ccc333"])
    picks = [ring.active()[0] for _ in range(6)]
    assert picks == [0, 1, 2, 0, 1, 2]


def test_cooled_key_is_skipped_then_recovers():
    ring = MemoryKeyRing(["aaa111", "bbb222"])
    ring.cool(0, seconds=60)
    idx, _ = ring.active()
    assert idx == 1
    ring.cool(0, seconds=0)          # cooldown elapsed
    picks = {ring.active()[0] for _ in range(4)}
    assert picks == {0, 1}


def test_all_cooling_returns_none_with_recovery_eta():
    ring = MemoryKeyRing(["aaa111"])
    ring.cool(0, seconds=QUOTA_COOL)
    assert ring.active() is None
    assert ring.seconds_until_recovery() > 0


def test_fingerprint_never_reveals_key():
    assert fingerprint("sgai-secret-abcdef") == "abcdef"
    assert len(fingerprint("xy")) == 6
