"""DRF glue for signed machine-to-machine webhooks (see :mod:`apps.core.signing`).

Every rejection is counted (``jt_webhooks_received_total`` and
``jt_security_events_total``) and recorded in the audit pipeline as a
platform-level security event. Verification must run before ``request.data``
is read, because it signs the raw body.
"""

from __future__ import annotations

from django.conf import settings
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.metrics import SECURITY_EVENTS, WEBHOOKS
from apps.core.signing import SignatureError, verify_request

_STATUS = {
    "not_configured": status.HTTP_503_SERVICE_UNAVAILABLE,
    "replayed": status.HTTP_409_CONFLICT,
}


def reject_unsigned(request: Request, *, source: str, secrets_: list[str]) -> Response | None:
    """Return an error response for an unauthenticated request, else ``None``."""
    try:
        verify_request(
            body=request.body,
            headers=request.headers,
            secrets_=secrets_,
            namespace=source,
            tolerance_seconds=settings.WEBHOOK_REPLAY_TOLERANCE_SECONDS,
        )
    except SignatureError as exc:
        WEBHOOKS.labels(source, exc.code).inc()
        if exc.code != "not_configured":
            from apps.governance.audit import security_event

            SECURITY_EVENTS.labels(f"webhook_{exc.code}").inc()
            security_event(
                "webhook.rejected",
                resource_type=source,
                description=f"Rejected {source} webhook: {exc.code}",
                request=request._request,
                reason=exc.code,
            )
        return Response(
            {"detail": str(exc), "code": exc.code}, status=_STATUS.get(exc.code, status.HTTP_401_UNAUTHORIZED)
        )
    WEBHOOKS.labels(source, "accepted").inc()
    return None


def n8n_callback_secrets() -> list[str]:
    """Secrets accepted on n8n->Django callbacks; the previous one stays valid during rotation."""
    return [
        secret
        for secret in (settings.N8N_WEBHOOK_SECRET, getattr(settings, "N8N_WEBHOOK_SECRET_PREVIOUS", ""))
        if secret
    ]
