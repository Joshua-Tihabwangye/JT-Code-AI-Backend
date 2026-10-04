"""API throttles backed by :mod:`apps.core.ratelimit` (atomic, Redis).

Every scoped throttle enforces three dimensions: the client IP (``THROTTLE_IP``),
the user (the scope's rate) and the tenant (the scope's rate multiplied by
``THROTTLE_TENANT_MULTIPLIER`` or the plan's ``rate_multiplier``). The IP
throttle is also the default for views without a scoped throttle.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.cache import caches
from rest_framework.request import Request
from rest_framework.throttling import BaseThrottle

from apps.core.ratelimit import hit, parse_rate


def _rate(scope: str) -> str | None:
    rates: dict[str, str | None] = dict(
        getattr(settings, "REST_FRAMEWORK", {}).get("DEFAULT_THROTTLE_RATES", {})
    )
    return rates.get(scope)


def _client_ip(request: Request) -> str:
    return str(request.META.get("REMOTE_ADDR") or "unknown")


def _tenant_multiplier(organization: Any) -> int:
    cache = caches["rate_limits"]
    key = f"rl-plan:{organization.id}"
    cached = cache.get(key)
    if cached is not None:
        return int(cached)
    from apps.usage.services import plan_limit

    multiplier = plan_limit(organization, "rate_multiplier", settings.THROTTLE_TENANT_MULTIPLIER)
    cache.set(key, multiplier, timeout=60)
    return multiplier


def _organization(request: Request) -> Any:
    from apps.identity.authorization import organization_for_request

    try:
        return organization_for_request(request)
    except Exception:  # noqa: BLE001 - authorization errors surface in the view, not the throttle
        return None


class _Throttle(BaseThrottle):
    scope = ""

    def __init__(self) -> None:
        self._wait = 0.0

    def _check(self, key: str, rate: str | None, *, multiplier: int = 1) -> bool:
        if not rate:
            return True
        limit, window = parse_rate(rate)
        decision = hit(key, limit=limit * multiplier, window=window)
        if not decision.allowed:
            self._wait = max(self._wait, decision.retry_after)
        return decision.allowed

    def wait(self) -> float | None:
        return self._wait or None


class IPRateThrottle(_Throttle):
    """Per-client-IP ceiling for every request (including anonymous ones)."""

    scope = "ip"

    def allow_request(self, request: Request, view: Any) -> bool:
        return self._check(f"ip:{_client_ip(request)}", _rate("ip"))


class PerUserRateThrottle(_Throttle):
    """Scoped limits per IP, per user and per tenant."""

    def allow_request(self, request: Request, view: Any) -> bool:
        rate = _rate(self.scope)
        if not self._check(f"ip:{_client_ip(request)}", _rate("ip")):
            return False
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return True
        if not self._check(f"{self.scope}:user:{user.id}", rate):
            return False
        organization = _organization(request)
        if organization is None:
            return True
        return self._check(
            f"{self.scope}:org:{organization.id}", rate, multiplier=_tenant_multiplier(organization)
        )


class ChatThrottle(PerUserRateThrottle):
    scope = "chat"


class ImageThrottle(PerUserRateThrottle):
    scope = "images"


class EmbeddingThrottle(PerUserRateThrottle):
    scope = "embeddings"


class ConversionThrottle(PerUserRateThrottle):
    scope = "conversions"


class ResearchThrottle(PerUserRateThrottle):
    scope = "research"


class BurstThrottle(PerUserRateThrottle):
    scope = "burst"


class AgentRunThrottle(PerUserRateThrottle):
    scope = "agent_runs"


class AnalyticsThrottle(PerUserRateThrottle):
    scope = "analytics"
