"""Circuit breaking, retries and deadlines for provider calls.

The circuit breaker state lives in the shared Django cache (Redis in deployed
environments) so every API and worker process sees the same provider health.
Cache outages fail *open*: losing breaker state must never stop generation.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

from django.conf import settings
from django.core.cache import cache

from apps.ai_gateway.providers.base import CircuitOpen, ProviderError, ProviderRateLimited, ProviderTimeout

logger = logging.getLogger(__name__)

# Indirection so tests can run retries without sleeping.
sleep = time.sleep


def _keys(provider_slug: str) -> tuple[str, str]:
    return f"ai-circuit:{provider_slug}:failures", f"ai-circuit:{provider_slug}:open-until"


class CircuitBreaker:
    """Consecutive-failure breaker: closed → open (cooldown) → half-open trial → closed."""

    def __init__(self, provider_slug: str, *, threshold: int, cooldown_seconds: int):
        self.provider_slug = provider_slug
        self.threshold = max(1, threshold)
        self.cooldown_seconds = max(1, cooldown_seconds)
        self._failures_key, self._open_key = _keys(provider_slug)

    @classmethod
    def for_provider(cls, provider: object) -> CircuitBreaker:
        return cls(
            str(getattr(provider, "slug", "") or getattr(provider, "type", "unknown")),
            threshold=int(getattr(provider, "circuit_breaker_threshold", 0) or 5),
            cooldown_seconds=settings.AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        )

    def state(self) -> str:
        try:
            open_until = cache.get(self._open_key)
        except Exception:  # noqa: BLE001 - fail open
            return "closed"
        if open_until is None:
            return "closed"
        return "open" if float(open_until) > time.time() else "half_open"

    def before_call(self) -> None:
        """Raise :class:`CircuitOpen` while the provider is cooling down."""
        if self.state() == "open":
            raise CircuitOpen(f"Provider {self.provider_slug!r} circuit is open; skipping.")

    def record_success(self) -> None:
        try:
            cache.delete_many([self._failures_key, self._open_key])
        except Exception:  # noqa: BLE001 - fail open
            logger.warning("circuit breaker reset failed", extra={"dependency": "cache"})

    def record_failure(self, error: ProviderError) -> None:
        if not error.counts_against_circuit:
            return
        try:
            if self.state() == "half_open":
                self._trip()
                return
            cache.add(self._failures_key, 0, timeout=self.cooldown_seconds * 10)
            failures = cache.incr(self._failures_key)
            if failures >= self.threshold:
                self._trip()
        except Exception:  # noqa: BLE001 - fail open
            logger.warning("circuit breaker update failed", extra={"dependency": "cache"})

    def _trip(self) -> None:
        cache.set(self._open_key, time.time() + self.cooldown_seconds, timeout=self.cooldown_seconds * 10)
        cache.delete(self._failures_key)
        logger.warning("provider circuit opened", extra={"dependency": self.provider_slug})


def backoff_seconds(attempt: int) -> float:
    """Exponential backoff with full jitter, bounded by ``AI_RETRY_MAX_BACKOFF_SECONDS``."""
    ceiling = min(
        float(settings.AI_RETRY_MAX_BACKOFF_SECONDS),
        float(settings.AI_RETRY_BASE_SECONDS) * (2**attempt),
    )
    return random.uniform(0, ceiling)  # nosec B311 - retry jitter, not security


class Deadline:
    """Wall-clock budget for one gateway request across retries and fallbacks."""

    def __init__(self, milliseconds: int):
        self.expires_at = time.monotonic() + max(1, milliseconds) / 1000

    def remaining(self) -> float:
        return self.expires_at - time.monotonic()

    def check(self) -> None:
        if self.remaining() <= 0:
            raise ProviderTimeout("The AI gateway request deadline was exceeded.")


def call_with_retry[T](
    operation: Callable[[], T],
    *,
    max_retries: int,
    deadline: Deadline,
    on_retry: Callable[[int, ProviderError], None] | None = None,
) -> T:
    """Run ``operation``; retry retryable provider errors with bounded backoff.

    ``Retry-After`` is honoured when it fits inside the backoff ceiling and the
    deadline; a longer hint is treated as "try another model instead".
    """
    attempt = 0
    while True:
        deadline.check()
        try:
            return operation()
        except ProviderError as exc:
            if not exc.retryable or attempt >= max_retries:
                raise
            delay = backoff_seconds(attempt)
            if isinstance(exc, ProviderRateLimited) and exc.retry_after is not None:
                if exc.retry_after > settings.AI_RETRY_MAX_BACKOFF_SECONDS:
                    raise
                delay = exc.retry_after
            if delay >= deadline.remaining():
                raise
            attempt += 1
            if on_retry is not None:
                on_retry(attempt, exc)
            sleep(delay)
