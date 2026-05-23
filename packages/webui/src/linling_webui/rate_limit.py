"""Minimal in-process rate limiter.

A fixed-window counter keyed by (bucket, identifier). Sufficient for the
WebUI's login endpoint and per-user write limits without adding a Redis
dependency.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class _Window:
    count: int = 0
    resets_at: float = 0.0


class RateLimiter:
    """Fixed-window rate limiter.

    `check(bucket, key, limit, window_s)` returns True if within quota;
    False if the caller should be denied.

    Windows are pruned opportunistically: every ``_GC_EVERY`` ``check``
    calls we walk the map and drop entries whose window expired and
    whose count is zero (i.e. nobody re-acquired since reset). This
    bounds memory under heavy traffic from many distinct keys without
    adding a separate timer.
    """

    _GC_EVERY = 1024

    def __init__(self) -> None:
        self._windows: dict[tuple[str, str], _Window] = defaultdict(_Window)
        self._calls_since_gc = 0

    def check(self, bucket: str, key: str, limit: int, window_s: float) -> bool:
        now = time.monotonic()
        w = self._windows[(bucket, key)]
        if now >= w.resets_at:
            w.count = 0
            w.resets_at = now + window_s
        if w.count >= limit:
            return False
        w.count += 1
        self._calls_since_gc += 1
        if self._calls_since_gc >= self._GC_EVERY:
            self._calls_since_gc = 0
            self._gc(now)
        return True

    def retry_after(self, bucket: str, key: str) -> float:
        w = self._windows.get((bucket, key))
        if w is None:
            return 0.0
        return max(0.0, w.resets_at - time.monotonic())

    def _gc(self, now: float) -> None:
        """Drop windows whose deadline passed without further acquires.

        We keep windows that are *still active* (count > 0 within the
        current second) so retry_after stays accurate; everything else
        is reconstructed cheaply on the next ``check``.
        """
        stale = [k for k, w in self._windows.items() if now >= w.resets_at]
        for k in stale:
            self._windows.pop(k, None)
