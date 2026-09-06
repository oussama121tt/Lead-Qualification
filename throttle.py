"""Human-mimicking pacing for the LinkedIn lane, and interruptible sleeps.

Ported from the proven lead_tool enrichment project (orchestrator/throttle.py).
A Stop request is honored within ~0.5s even during a multi-minute pause.
"""
from __future__ import annotations

import random
import threading
import time


def sleep_interruptible(seconds: float, stop: threading.Event | None = None,
                        slice_s: float = 0.5) -> bool:
    """Sleep in slices, checking `stop`. Returns True if it slept the full time,
    False if interrupted by Stop."""
    if seconds <= 0:
        return not (stop and stop.is_set())
    remaining = seconds
    while remaining > 0:
        if stop and stop.is_set():
            return False
        if stop is not None:
            if stop.wait(min(slice_s, remaining)):   # event set during wait
                return False
        else:
            time.sleep(min(slice_s, remaining))
        remaining -= slice_s
    return True


class Pacer:
    """Tracks profile count and yields the delay before the next profile,
    including the periodic long pause. Values come from runconfig.

    cfg must expose: delay_min, delay_max, long_pause_every_min,
    long_pause_every_max, long_pause_min, long_pause_max.
    """

    def __init__(self, cfg_linkedin):
        self.cfg = cfg_linkedin
        self._done = 0
        self._next_long_at = random.randint(
            cfg_linkedin.long_pause_every_min, cfg_linkedin.long_pause_every_max)

    def profile_delay(self) -> float:
        return random.uniform(self.cfg.delay_min, self.cfg.delay_max)

    def note_profile_done(self) -> float:
        """Call after each profile. Returns an ADDITIONAL long-pause duration
        (seconds) if it's time for one, else 0."""
        self._done += 1
        if self._done >= self._next_long_at:
            self._next_long_at = self._done + random.randint(
                self.cfg.long_pause_every_min, self.cfg.long_pause_every_max)
            return random.uniform(self.cfg.long_pause_min, self.cfg.long_pause_max)
        return 0.0
