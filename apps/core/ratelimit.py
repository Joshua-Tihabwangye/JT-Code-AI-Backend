"""Atomic sliding-window rate limiting on the ``rate_limits`` cache (Redis).

Each window is a counter created with ``add`` (``SET NX``) and incremented with
``incr`` (``INCR``), both atomic on Redis, so concurrent requests can never
both observe the last free slot (unlike read-modify-write throttles). The
sliding estimate weights the previous window by its remaining overlap.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass

from django.core.cache import caches
from django.core.cache.backends.base import BaseCache


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: float


def _increment(cache: BaseCache, key: str, ttl: int) -> int:
    for _attempt in range(3):
        cache.add(key, 0, timeout=ttl)
        try:
            return int(cache.incr(key))
        except ValueError:
            continue  # expired between add and incr; recreate it
    return int(cache.get(key) or 0)


def hit(key: str, *, limit: int, window: int, now: float | None = None) -> Decision:
    """Count one request against ``key``; deny once the sliding count exceeds ``limit``."""
    cache = caches["rate_limits"]
    moment = time.time() if now is None else now
    bucket = int(moment // window)
    elapsed = (moment % window) / window
    current = _increment(cache, f"rl:{key}:{bucket}", window * 2)
    previous = int(cache.get(f"rl:{key}:{bucket - 1}") or 0)
    estimate = previous * (1 - elapsed) + current
    if estimate <= limit:
        return Decision(True, limit, max(0, int(limit - estimate)), 0.0)
    # Undo the attempt so a denied burst does not lengthen the lockout.
    with contextlib.suppress(ValueError):
        cache.decr(f"rl:{key}:{bucket}")
    return Decision(False, limit, 0, max(1.0, (1 - elapsed) * window))


def parse_rate(rate: str) -> tuple[int, int]:
    """``"60/hour"`` → (60, 3600)."""
    count, period = rate.split("/", 1)
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[period.strip()[0].lower()]
    return int(count), seconds
