"""API-key rotation with a recovering cooling pool.

Ported from the proven lead_tool enrichment project (orchestrator/keyring.py).

Rules:
  - On 429: back off + retry the SAME key first, then cool it BRIEFLY (recovers).
  - On 401/402/403 (out of credits / forbidden): cool it for a LONG time
    (effectively excluded for the run) and move on.
  - Cooled keys return automatically once their cooldown elapses.
  - Only when NO key can recover soon do we signal exhaustion.
  - Log only the key index + last-6 fingerprint, never the secret.

MemoryKeyRing — thread-safe, round-robin so N concurrent workers get DIFFERENT
keys; timed cooldowns with automatic recovery. State lives for the process
lifetime, which matches how the Flask app runs (one long-lived process).
"""
from __future__ import annotations

import threading
import time

RATE_COOL = 30.0      # seconds to cool a key after a persistent 429 (recovers)
QUOTA_COOL = 3600.0   # seconds to cool a key that is out of credits / forbidden


class AllKeysExhausted(RuntimeError):
    """No active key remains (all cooling / quota-exhausted)."""


def fingerprint(key: str) -> str:
    """Last 6 chars, for display/logging. Never expose the full key."""
    return key[-6:] if len(key) >= 6 else "??????"


class MemoryKeyRing:
    def __init__(self, keys: list[str], default_cool: float = RATE_COOL):
        if not keys:
            raise ValueError("need at least one key")
        self._keys = keys
        self._cool_until = [0.0] * len(keys)   # monotonic deadline per key
        self._default = default_cool
        self._rr = 0
        self._lock = threading.Lock()

    def active(self) -> tuple[int, str] | None:
        with self._lock:
            now = time.monotonic()
            n = len(self._keys)
            for step in range(n):
                i = (self._rr + step) % n
                if self._cool_until[i] <= now:
                    self._rr = (i + 1) % n     # round-robin: spread across workers
                    return i, self._keys[i]
            return None

    def cool(self, idx: int, seconds: float | None = None) -> None:
        with self._lock:
            self._cool_until[idx] = time.monotonic() + (
                seconds if seconds is not None else self._default)

    def note_used(self, idx: int) -> None:
        pass

    def seconds_until_recovery(self) -> float:
        with self._lock:
            now = time.monotonic()
            future = [c - now for c in self._cool_until if c > now]
            return min(future) if future else 0.0

    def status(self) -> list[dict]:
        with self._lock:
            now = time.monotonic()
            return [{"idx": i, "fingerprint": fingerprint(k),
                     "state": "cooling" if self._cool_until[i] > now else "active"}
                    for i, k in enumerate(self._keys)]
