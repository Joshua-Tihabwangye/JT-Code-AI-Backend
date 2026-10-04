"""Edge protection: client IP resolution, Cloudflare origin lock and security headers.

**Client IP.** ``REMOTE_ADDR`` is the last proxy, not the client. The IP used
by rate limits and audit records is resolved in this order:

1. ``CF-Connecting-IP`` - only when the request provably came through
   Cloudflare (the origin-auth header injected by a Cloudflare Transform Rule
   matches ``CLOUDFLARE_ORIGIN_SECRET``); otherwise the header is attacker
   controlled and ignored.
2. ``X-Forwarded-For`` - the entry ``TRUSTED_PROXY_HOPS`` positions from the
   right, i.e. the address appended by the outermost proxy we operate. Entries
   further left are client supplied and never trusted.
3. ``REMOTE_ADDR``.

**Origin lock.** With ``CLOUDFLARE_ENFORCE_ORIGIN`` every request except health
probes must carry the origin-auth header, so the WAF cannot be bypassed by
calling the origin directly (see ``infra/cloudflare``).

**Security headers.** A restrictive CSP for API responses (JSON never needs to
load anything), a separate policy for the admin and API docs, Permissions-Policy,
CORP, and ``Cache-Control: no-store`` on authenticated responses.
"""

from __future__ import annotations

import ipaddress
import secrets
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse

ORIGIN_AUTH_HEADER = "X-JT-Origin-Auth"
_EXEMPT_ORIGIN_PATHS = ("/api/v1/health/",)
_HTML_PREFIXES = ("/admin/", "/api/docs/", "/api/v1/docs/")


def _valid_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def via_cloudflare(request: HttpRequest) -> bool:
    secret = getattr(settings, "CLOUDFLARE_ORIGIN_SECRET", "")
    supplied = request.headers.get(ORIGIN_AUTH_HEADER, "")
    return bool(secret and supplied and secrets.compare_digest(supplied, secret))


def client_ip(request: HttpRequest) -> str:
    cached = getattr(request, "_jt_client_ip", None)
    if cached:
        return str(cached)
    resolved = None
    if via_cloudflare(request):
        resolved = _valid_ip(request.headers.get("CF-Connecting-IP", ""))
    hops = int(getattr(settings, "TRUSTED_PROXY_HOPS", 0) or 0)
    if resolved is None and hops > 0:
        chain = [item for item in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if item.strip()]
        if len(chain) >= hops:
            resolved = _valid_ip(chain[-hops])
    if resolved is None:
        resolved = _valid_ip(str(request.META.get("REMOTE_ADDR") or "")) or "unknown"
    request._jt_client_ip = resolved  # type: ignore[attr-defined]
    return resolved


class EdgeProtectionMiddleware:
    """Reject requests that bypassed Cloudflare; resolve the client IP once."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if (
            getattr(settings, "CLOUDFLARE_ENFORCE_ORIGIN", False)
            and not request.path.startswith(_EXEMPT_ORIGIN_PATHS)
            and not via_cloudflare(request)
        ):
            from apps.core.metrics import SECURITY_EVENTS

            SECURITY_EVENTS.labels("origin_bypass").inc()
            return JsonResponse({"detail": "Direct origin access is not allowed."}, status=403)
        client_ip(request)
        return self.get_response(request)


class SecurityHeadersMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        html = request.path.startswith(_HTML_PREFIXES)
        policy = settings.CSP_HTML_POLICY if html else settings.CSP_API_POLICY
        response.headers.setdefault("Content-Security-Policy", policy)
        response.headers.setdefault("Permissions-Policy", settings.PERMISSIONS_POLICY)
        response.headers.setdefault("Cross-Origin-Resource-Policy", settings.CROSS_ORIGIN_RESOURCE_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if "Authorization" in request.headers or request.COOKIES.get(settings.SESSION_COOKIE_NAME):
            response.headers.setdefault("Cache-Control", "no-store")
        return response
