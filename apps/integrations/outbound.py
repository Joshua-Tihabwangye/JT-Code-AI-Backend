"""Outbound webhooks: selected domain events, delivered to an organization's HTTPS endpoints.

**Which events.** Only the event types in :data:`WEBHOOK_EVENTS`. Each names how its organization is
found from the entity the event is about (the event key), never from a payload field. An event whose
organization cannot be determined is not delivered (fail closed), so no tenant receives another's events.

**When.** :func:`fan_out` runs inside the producer's transaction (``add_outbox_event``) and creates one
:class:`WebhookDelivery` per subscribed active webhook, so a delivery exists exactly when the change
commits. The delivery task runs after commit; a beat sweep re-dispatches due deliveries, so a lost broker
message delays a delivery but never loses it.

**How.** ``POST`` of a JSON envelope through the SSRF-safe egress layer (``apps.tools.egress``): HTTPS
only, public addresses only, pinned connection, no redirects, capped response, timeout. Signed like
Stripe: ``X-JT-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>``; ``X-JT-Delivery`` is the
idempotency key. 2xx is delivered; anything else is retried with backoff until ``max_attempts``.
``WEBHOOK_DISABLE_AFTER_FAILURES`` failed deliveries in a row disable the endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-JT-Signature"
TEST_EVENT = "webhook.test"
# Headers a webhook's own ``headers`` may not set: framing, routing and our signature.
RESERVED_HEADERS = frozenset(
    {"host", "content-type", "content-length", "transfer-encoding", "connection", "user-agent"}
)
RESERVED_PREFIX = "x-jt-"
# Producer payload keys that are internal and never leave the platform.
_INTERNAL_KEYS = frozenset({"traceId", "trace_id", "modelRunId", "headers"})


def _org(
    model_path: str, field: str = "organization_id", lookup: str = "id"
) -> Callable[[str, dict], str | None]:
    def resolve(event_key: str, payload: dict) -> str | None:
        from django.apps import apps
        from django.core.exceptions import ValidationError

        model = apps.get_model(*model_path.split("."))
        try:
            value = model.objects.filter(**{lookup: event_key}).values_list(field, flat=True).first()
        except ValueError, TypeError, ValidationError:
            return None
        return str(value) if value else None

    return resolve


def _image_org(event_key: str, payload: dict) -> str | None:
    return _org("ai_gateway.ImageGeneration")(str(payload.get("generationId", "")), payload)


# Event type -> how to find its organization. Adding a type here publishes it to customers.
WEBHOOK_EVENTS: dict[str, Callable[[str, dict], str | None]] = {
    "chat.request.completed": _org("conversations.ChatRequest"),
    "chat.request.failed": _org("conversations.ChatRequest"),
    "jobs.job.completed": _org("jobs.Job", lookup="request_id"),
    "jobs.job.failed": _org("jobs.Job", lookup="request_id"),
    "document.render.completed": _org("documents.Document"),
    "document.render.failed": _org("documents.Document"),
    "conversion.job.completed": _org("conversions.ConversionJob"),
    "conversion.job.failed": _org("conversions.ConversionJob"),
    "knowledge.document.indexed": _org("knowledge.Document", field="collection__organization_id"),
    "knowledge.document.index_failed": _org("knowledge.Document", field="collection__organization_id"),
    "images.generated": _image_org,
    "agents.run.completed": _org("agents.AgentRun"),
    "agents.run.failed": _org("agents.AgentRun"),
}


def subscribed(webhook: Any, event_type: str) -> bool:
    from apps.integrations.models import Webhook

    if webhook.event_filter == Webhook.EventFilter.ALL:
        return True
    return event_type in (webhook.events or [])


def envelope(event_id: str, event_type: str, organization_id: str, data: dict) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": event_type,
        "created_at": timezone.now().isoformat(),
        "organization_id": organization_id,
        "data": {key: value for key, value in data.items() if key not in _INTERNAL_KEYS},
    }


def fan_out(outbox_event: Any, event_type: str) -> list[Any]:
    """Create deliveries for ``outbox_event`` (called inside the producer's transaction)."""
    resolver = WEBHOOK_EVENTS.get(event_type)
    if resolver is None:
        return []
    from apps.integrations.models import Webhook, WebhookDelivery

    payload = outbox_event.payload if isinstance(outbox_event.payload, dict) else {}
    organization_id = resolver(str(outbox_event.event_key), payload)
    if not organization_id:
        return []
    created = []
    for webhook in Webhook.objects.filter(organization_id=organization_id, status=Webhook.Status.ACTIVE):
        if not subscribed(webhook, event_type):
            continue
        delivery = WebhookDelivery.objects.create(
            webhook=webhook,
            event_type=event_type,
            payload=envelope(str(outbox_event.id), event_type, organization_id, payload),
            max_attempts=settings.WEBHOOK_MAX_ATTEMPTS,
            next_retry_at=timezone.now(),
        )
        created.append(delivery)
        schedule(delivery.id)
    return created


def create_test_delivery(webhook: Any) -> Any:
    """A one-attempt ``webhook.test`` delivery (also to a paused endpoint, so it can be checked first)."""
    from apps.integrations.models import WebhookDelivery

    delivery = WebhookDelivery.objects.create(
        webhook=webhook,
        event_type=TEST_EVENT,
        payload=envelope(
            str(uuid.uuid4()),
            TEST_EVENT,
            str(webhook.organization_id),
            {"message": "Test event from JT-Code."},
        ),
        max_attempts=1,
        next_retry_at=timezone.now(),
    )
    schedule(delivery.id)
    return delivery


def schedule(delivery_id: Any, countdown: float = 0) -> None:
    def dispatch() -> None:
        from apps.integrations.tasks import deliver_webhook

        try:
            deliver_webhook.apply_async(args=[str(delivery_id)], countdown=countdown)
        except Exception:  # noqa: BLE001 - the beat sweep re-dispatches due deliveries
            logger.warning("webhook delivery dispatch failed; the sweep will retry", exc_info=True)

    transaction.on_commit(dispatch)


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    timestamp = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def backoff_seconds(attempt: int) -> int:
    """30 s, 2 min, 8 min, 32 min, ... capped at 6 h."""
    return int(min(6 * 3600, 30 * 4 ** max(0, attempt - 1)))


def custom_headers(webhook: Any) -> dict[str, str]:
    headers = webhook.headers if isinstance(webhook.headers, dict) else {}
    return {
        str(name): str(value)
        for name, value in headers.items()
        if str(name).lower() not in RESERVED_HEADERS and not str(name).lower().startswith(RESERVED_PREFIX)
    }


def deliver(delivery_id: str) -> str:
    """Attempt one delivery; return its new status. Safe to call twice (locked, rechecked, leased)."""
    from apps.integrations.models import Webhook, WebhookDelivery
    from apps.tools.egress import EgressDenied, EgressError, safe_request

    with transaction.atomic():
        delivery = (
            WebhookDelivery.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("webhook")
            .filter(id=delivery_id)
            .first()
        )
        if delivery is None:
            return "busy-or-missing"
        if delivery.status in (WebhookDelivery.Status.DELIVERED, WebhookDelivery.Status.FAILED):
            return delivery.status
        webhook = delivery.webhook
        if webhook.status != Webhook.Status.ACTIVE and delivery.event_type != TEST_EVENT:
            delivery.status = WebhookDelivery.Status.FAILED
            delivery.error_message = f"The endpoint is {webhook.status}."
            delivery.next_retry_at = None
            delivery.save(update_fields=["status", "error_message", "next_retry_at"])
            return delivery.status
        claimed_at = timezone.now()
        if delivery.next_retry_at and delivery.next_retry_at > claimed_at:
            return "not-due"  # another attempt holds the lease, or a retry is scheduled later
        delivery.attempt += 1
        # The lease: a concurrent or duplicate task skips it; if this worker dies, the sweep retries after it.
        delivery.next_retry_at = claimed_at + timedelta(seconds=settings.WEBHOOK_TIMEOUT_SECONDS + 60)
        delivery.save(update_fields=["attempt", "next_retry_at"])

    body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode()
    headers = {
        **custom_headers(webhook),
        "Content-Type": "application/json",
        "User-Agent": "JT-Code-Webhooks/1",
        "X-JT-Event": delivery.event_type,
        "X-JT-Delivery": str(delivery.id),
        SIGNATURE_HEADER: sign(webhook.secret, body),
    }
    status_code, response_body, response_headers, error = None, "", {}, ""
    try:
        response = safe_request(
            "POST",
            webhook.url,
            headers=headers,
            content=body,
            timeout=settings.WEBHOOK_TIMEOUT_SECONDS,
            max_bytes=64 * 1024,
            max_redirects=0,
        )
        status_code = response.status_code
        response_body = response.content[:1024].decode("utf-8", errors="replace")
        response_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in {"content-type", "retry-after"}
        }
        if not 200 <= status_code < 300:
            error = f"The endpoint answered HTTP {status_code}."
    except EgressDenied as exc:
        error = f"Refused: {exc}"
    except EgressError as exc:
        error = str(exc)

    now = timezone.now()
    with transaction.atomic():
        delivery = WebhookDelivery.objects.select_for_update().get(id=delivery.id)
        delivery.response_status = status_code
        delivery.response_body = response_body
        delivery.response_headers = response_headers
        delivery.error_message = error
        delivery.next_retry_at = None
        updates = [
            "response_status",
            "response_body",
            "response_headers",
            "error_message",
            "status",
            "next_retry_at",
        ]
        if not error:
            delivery.status = WebhookDelivery.Status.DELIVERED
            delivery.delivered_at = now
            updates.append("delivered_at")
            Webhook.objects.filter(id=webhook.id).update(
                last_triggered_at=now, last_success_at=now, failure_count=0, is_verified=True, updated_at=now
            )
        elif delivery.attempt < delivery.max_attempts:
            delivery.status = WebhookDelivery.Status.RETRYING
            delivery.next_retry_at = now + timedelta(seconds=backoff_seconds(delivery.attempt))
            Webhook.objects.filter(id=webhook.id).update(
                last_triggered_at=now, last_failure_at=now, updated_at=now
            )
        else:
            delivery.status = WebhookDelivery.Status.FAILED
            if delivery.event_type == TEST_EVENT:
                Webhook.objects.filter(id=webhook.id).update(last_failure_at=now, updated_at=now)
            else:
                Webhook.objects.filter(id=webhook.id).update(
                    last_triggered_at=now,
                    last_failure_at=now,
                    failure_count=F("failure_count") + 1,
                    updated_at=now,
                )
                Webhook.objects.filter(
                    id=webhook.id,
                    status=Webhook.Status.ACTIVE,
                    failure_count__gte=settings.WEBHOOK_DISABLE_AFTER_FAILURES,
                ).update(status=Webhook.Status.DISABLED, updated_at=now)
        delivery.save(update_fields=updates)
    if delivery.status == WebhookDelivery.Status.RETRYING:
        schedule(delivery.id, countdown=backoff_seconds(delivery.attempt))
    return delivery.status


def due_delivery_ids(limit: int = 500) -> list[str]:
    """Deliveries whose attempt is due: what the beat sweep re-dispatches."""
    from apps.integrations.models import WebhookDelivery

    return [
        str(value)
        for value in WebhookDelivery.objects.filter(
            status__in=(WebhookDelivery.Status.PENDING, WebhookDelivery.Status.RETRYING),
            next_retry_at__lte=timezone.now(),
        )
        .order_by("next_retry_at")
        .values_list("id", flat=True)[:limit]
    ]
