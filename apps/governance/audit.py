"""The audit event pipeline (Phase 15).

``record_audit_event`` is the only writer of :class:`AuditEvent`. In the
caller's transaction it

1. redacts secrets from the metadata,
2. stores the append-only row (a database trigger rejects later edits),
3. enqueues ``governance.audit.recorded`` on the transactional outbox, so the
   event reaches Kafka (and any SIEM consumer) exactly when the change commits,
4. emits a structured ``jt_code.audit`` log line and a Prometheus counter.

Sources: :class:`AuditTrailMiddleware` audits every mutating request to a
security-sensitive route (``AUDIT_ROUTE_RULES``), including denied attempts;
Django admin changes are audited through ``LogEntry``; security rejections
(forged or replayed webhooks) and tool gateway decisions call this directly.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse

from apps.core.context import request_id_var, trace_id_var
from apps.governance.models import AuditEvent

logger = logging.getLogger("jt_code.audit")
_SENSITIVE = re.compile(r"(secret|token|password|key|authorization|signature|card|cvc|credential)", re.I)
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def redact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _SENSITIVE.search(str(key)) else redact(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item, depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return value[:500]
    return value


def _request_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError, TypeError:
        return None


def record_audit_event(
    *,
    category: str,
    action: str,
    resource_type: str,
    resource_id: str = "",
    description: str = "",
    organization: Any = None,
    organization_id: Any = None,
    actor: Any = None,
    severity: str = AuditEvent.Severity.LOW,
    outcome: str = AuditEvent.Outcome.SUCCESS,
    metadata: dict[str, Any] | None = None,
    request: HttpRequest | None = None,
    trace_id: str = "",
) -> AuditEvent:
    from apps.core.edge import client_ip
    from apps.core.metrics import AUDIT_EVENTS
    from apps.events.outbox import enqueue_outbox_event

    if actor is not None and not getattr(actor, "is_authenticated", False):
        actor = None
    request_id = _request_uuid(request_id_var.get())
    trace = trace_id or (trace_id_var.get() if trace_id_var.get() != "-" else "")
    with transaction.atomic():
        event = AuditEvent.objects.create(
            organization=organization,
            organization_id=organization_id if organization is None else organization.id,
            actor=actor,
            category=category,
            action=action[:100],
            resource_type=resource_type[:100],
            resource_id=str(resource_id or "")[:255],
            severity=severity,
            outcome=outcome,
            description=description or action,
            metadata=redact(metadata or {}),
            ip_address=(ip if (ip := client_ip(request)) != "unknown" else None) if request else None,
            user_agent=(request.headers.get("User-Agent", "")[:500] if request else ""),
            trace_id=trace[:100],
            request_id=request_id,
        )
        enqueue_outbox_event(
            topic="governance.audit.recorded",
            event_key=str(event.organization_id or "platform"),
            payload={
                "audit_event_id": str(event.id),
                "organization_id": str(event.organization_id) if event.organization_id else None,
                "actor_id": str(event.actor_id) if event.actor_id else None,
                "category": event.category,
                "action": event.action,
                "resource_type": event.resource_type,
                "resource_id": event.resource_id,
                "severity": event.severity,
                "outcome": event.outcome,
                "occurred_at": event.created_at.isoformat(),
            },
        )
    AUDIT_EVENTS.labels(event.category, event.severity).inc()
    logger.info(
        "audit event",
        extra={
            "audit_event_id": str(event.id),
            "audit_category": event.category,
            "audit_action": event.action,
            "audit_outcome": event.outcome,
            "organization_id": str(event.organization_id or ""),
        },
    )
    return event


def security_event(
    action: str, *, resource_type: str, description: str, request: HttpRequest | None = None, **metadata: Any
) -> None:
    """Record a platform-level rejection (no tenant); never raises into the caller."""
    try:
        record_audit_event(
            category=AuditEvent.Category.SECURITY,
            action=action,
            resource_type=resource_type,
            description=description,
            severity=AuditEvent.Severity.HIGH,
            outcome=AuditEvent.Outcome.DENIED,
            metadata=metadata,
            request=request,
        )
    except Exception:  # noqa: BLE001 - auditing must not turn a 401 into a 500
        logger.exception("failed to record security audit event")


# Route-based auditing -------------------------------------------------------


def _compiled_rules() -> list[tuple[re.Pattern[str], str, str, frozenset[str]]]:
    return [
        (re.compile(pattern), category, severity, frozenset(methods.upper().split(",")))
        for pattern, category, severity, methods in getattr(settings, "AUDIT_ROUTE_RULES", ())
    ]


class AuditTrailMiddleware:
    """Audit mutating requests to sensitive routes, successful or denied."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.rules = _compiled_rules()

    def _rule(self, route: str, method: str) -> tuple[str, str] | None:
        for pattern, category, severity, methods in self.rules:
            if pattern.search(route) and ("*" in methods or method in methods):
                return category, severity
        return None

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if request.method not in _MUTATING or response.status_code >= 500:
            return response
        from apps.core.metrics import route_template

        match = getattr(request, "resolver_match", None)
        route = route_template(match)
        if not route or (rule := self._rule(route, request.method or "")) is None:
            return response
        if response.status_code < 400:
            outcome = AuditEvent.Outcome.SUCCESS
        elif response.status_code in (401, 403):
            outcome = AuditEvent.Outcome.DENIED
        else:
            return response  # validation errors change nothing
        try:
            self._record(request, response, match, route, rule, outcome)
        except Exception:  # noqa: BLE001 - the change already committed; never fail the response
            logger.exception("failed to record audit event", extra={"route": route})
        return response

    @staticmethod
    def _record(
        request: HttpRequest,
        response: HttpResponse,
        match: Any,
        route: str,
        rule: tuple[str, str],
        outcome: str,
    ) -> None:
        from apps.identity.authorization import organization_for_request

        category, severity = rule
        user = getattr(request, "user", None)
        organization = None
        if getattr(user, "is_authenticated", False):
            try:
                organization = organization_for_request(request)
            except Exception:  # noqa: BLE001 - a denied tenant header still deserves an audit row
                organization = None
        kwargs = match.kwargs or {}
        resource_id = next((str(kwargs[key]) for key in ("id", "pk", "slug", "job_id") if key in kwargs), "")
        view_name = match.view_name or route
        if outcome == AuditEvent.Outcome.DENIED:
            severity = AuditEvent.Severity.MEDIUM if severity == AuditEvent.Severity.LOW else severity
        record_audit_event(
            category=category if outcome == AuditEvent.Outcome.SUCCESS else AuditEvent.Category.AUTHORIZATION,
            action=f"{view_name}.{(request.method or '').lower()}",
            resource_type=view_name.rsplit("-", 1)[0] if "-" in view_name else view_name,
            resource_id=resource_id,
            description=f"{request.method} /{route} -> {response.status_code}",
            organization=organization,
            actor=user,
            severity=severity,
            outcome=outcome,
            metadata={"route": f"/{route}", "status": response.status_code},
            request=request,
        )


def audit_admin_log_entry(sender: Any, instance: Any, created: bool, **_: Any) -> None:
    """Mirror Django admin changes (``LogEntry``) into the audit pipeline."""
    if not created:
        return
    actions = {1: "admin.add", 2: "admin.change", 3: "admin.delete"}
    record_audit_event(
        category=AuditEvent.Category.ADMIN,
        action=actions.get(instance.action_flag, "admin.action"),
        resource_type=str(instance.content_type.model if instance.content_type_id else "unknown"),
        resource_id=str(instance.object_id or ""),
        description=f"Admin {actions.get(instance.action_flag, 'action')}: {instance.object_repr}"[:500],
        actor=instance.user,
        severity=AuditEvent.Severity.MEDIUM,
        metadata={"change": str(instance.change_message or "")[:500]},
    )
