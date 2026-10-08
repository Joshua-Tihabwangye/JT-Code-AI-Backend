"""Event-triggered workflows: domain events delivered to subscribed n8n workflows.

When an outbox event is written, every active workflow that lists its type in
``eventTypes`` gets a :class:`WorkflowEventDelivery` row *in the same
transaction* - so a delivery exists exactly when the domain change commits,
whether or not Kafka is reachable. Deliveries are then pushed to n8n with
signed requests and retried with backoff:

    PENDING -> DELIVERING -> ACCEPTED (n8n answered 2xx) -> COMPLETED (signed status callback)
                    \\-> retry (PENDING after backoff) ... -> FAILED (attempts exhausted)

An accepted delivery that never reports completion before its workflow
timeout is retried as well; workflows use ``deliveryId`` as their idempotency
key.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.core.metrics import WORKFLOW_DISPATCHES
from apps.core.tracing import inject_headers
from apps.events.outbox import enqueue_outbox_event
from apps.orchestration import client
from apps.orchestration.context import enrich
from apps.orchestration.models import WorkflowDefinition, WorkflowEventDelivery
from apps.orchestration.registry import definitions_for_event
from apps.orchestration.runs import ORCHESTRATION_QUEUE, backoff_seconds

logger = logging.getLogger(__name__)
_OPEN = (
    WorkflowEventDelivery.Status.PENDING,
    WorkflowEventDelivery.Status.DELIVERING,
    WorkflowEventDelivery.Status.ACCEPTED,
)


def fan_out(outbox_event: Any, event_type: str) -> list[WorkflowEventDelivery]:
    """Create deliveries for ``outbox_event`` (called inside the producer's transaction)."""
    if event_type.startswith("orchestration.") or not client.configured():
        return []
    definition_ids = definitions_for_event(event_type)
    if not definition_ids:
        return []
    payload = outbox_event.payload if isinstance(outbox_event.payload, dict) else {}
    organization_id = payload.get("organization_id")
    created = []
    for definition_id in definition_ids:
        delivery, was_created = WorkflowEventDelivery.objects.get_or_create(
            definition_id=definition_id,
            event_id=outbox_event.id,
            defaults={
                "event_type": event_type,
                "organization_id": organization_id if _is_uuid(organization_id) else None,
                "payload": payload,
                "headers": outbox_event.headers or {},
                "next_attempt_at": timezone.now(),
            },
        )
        if was_created:
            created.append(delivery)
            schedule_delivery(delivery.id)
    return created


def _is_uuid(value: Any) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return value is not None


def schedule_delivery(delivery_id: Any) -> None:
    from apps.orchestration.tasks import deliver_workflow_event

    def publish() -> None:
        try:
            deliver_workflow_event.apply_async(args=[str(delivery_id)], queue=ORCHESTRATION_QUEUE)
        except Exception:  # noqa: BLE001 - the sweeper delivers due rows from the database
            logger.warning("could not publish delivery task; the sweeper will retry", exc_info=True)

    transaction.on_commit(publish)


def build_event_payload(delivery: WorkflowEventDelivery) -> dict[str, Any]:
    callbacks = {"status": client.callback_url(f"n8n/deliveries/{delivery.id}/status/")}
    source_id = delivery.payload.get("sourceId")
    if delivery.event_type == "knowledge.integration.sync_requested" and source_id:
        callbacks["documents"] = client.callback_url(f"n8n/knowledge/sources/{source_id}/documents/")
    return {
        "kind": "event",
        "deliveryId": str(delivery.id),
        "eventId": str(delivery.event_id),
        "eventType": delivery.event_type,
        "occurredAt": delivery.created_at.isoformat(),
        "organizationId": str(delivery.organization_id) if delivery.organization_id else None,
        "attempt": delivery.attempts,
        "data": delivery.payload,
        "context": enrich(delivery.event_type, delivery.payload, delivery.organization),
        "callbacks": callbacks,
        "traceparent": (delivery.headers or {}).get("traceparent")
        or inject_headers({}).get("traceparent", ""),
    }


def deliver(delivery_id: Any) -> str:
    now = timezone.now()
    with transaction.atomic():
        delivery = (
            WorkflowEventDelivery.objects.select_for_update(of=("self",))
            .select_related("definition", "organization")
            .filter(id=delivery_id)
            .first()
        )
        if delivery is None:
            return "missing"
        if delivery.status != WorkflowEventDelivery.Status.PENDING:
            return "not_due"
        if delivery.next_attempt_at and delivery.next_attempt_at > now:
            return "not_due"
        definition = delivery.definition
        if not client.configured():
            delivery.status = WorkflowEventDelivery.Status.FAILED
            delivery.last_error = "n8n is not configured."
            delivery.save(update_fields=["status", "last_error", "updated_at"])
            return "not_configured"
        delivery.attempts += 1
        delivery.status = WorkflowEventDelivery.Status.DELIVERING
        delivery.next_attempt_at = now + timedelta(seconds=60)  # in-flight lease
        delivery.save(update_fields=["attempts", "status", "next_attempt_at", "updated_at"])
        attempt = delivery.attempts
        payload = build_event_payload(delivery)
    try:
        response = client.post_signed(
            definition.webhook_path, payload, idempotency_key=f"{delivery.id}:{attempt}"
        )
    except client.N8nError as exc:
        WORKFLOW_DISPATCHES.labels(definition.key, "retryable" if exc.retryable else "rejected").inc()
        return record_failed_delivery(delivery.id, attempt, str(exc), retryable=exc.retryable)
    WORKFLOW_DISPATCHES.labels(definition.key, "accepted").inc()
    accepted = client.response_json(response)
    with transaction.atomic():
        delivery = WorkflowEventDelivery.objects.select_for_update(of=("self",)).get(id=delivery.id)
        if delivery.attempts != attempt or delivery.status != WorkflowEventDelivery.Status.DELIVERING:
            return "superseded"
        delivery.status = WorkflowEventDelivery.Status.ACCEPTED
        delivery.response_status = response.status_code
        delivery.n8n_execution_id = str(accepted.get("executionId") or "")[:64]
        delivery.delivered_at = timezone.now()
        delivery.deadline_at = timezone.now() + timedelta(seconds=definition.timeout_seconds)
        delivery.next_attempt_at = None
        delivery.save()
        _delivery_event(delivery, "accepted")
    return "accepted"


def _delivery_event(delivery: WorkflowEventDelivery, name: str, **data: Any) -> None:
    enqueue_outbox_event(
        topic=f"orchestration.delivery.{name}",
        event_key=str(delivery.event_id),
        payload={
            "delivery_id": str(delivery.id),
            "event_id": str(delivery.event_id),
            "event_type": delivery.event_type,
            "workflow": delivery.definition.key if delivery.definition_id else "",
            "attempt": delivery.attempts,
            "organization_id": str(delivery.organization_id) if delivery.organization_id else None,
            **data,
        },
    )


def record_failed_delivery(delivery_id: Any, attempt: int, message: str, *, retryable: bool = True) -> str:
    with transaction.atomic():
        delivery = (
            WorkflowEventDelivery.objects.select_for_update(of=("self",))
            .select_related("definition")
            .filter(id=delivery_id)
            .first()
        )
        if delivery is None or delivery.attempts != attempt:
            return "stale"
        if delivery.status in (WorkflowEventDelivery.Status.COMPLETED, WorkflowEventDelivery.Status.FAILED):
            return "terminal"
        delivery.last_error = message[:2000]
        if retryable and delivery.attempts < delivery.definition.max_attempts:
            delay = backoff_seconds(delivery.attempts)
            delivery.status = WorkflowEventDelivery.Status.PENDING
            delivery.next_attempt_at = timezone.now() + timedelta(seconds=delay)
            delivery.deadline_at = None
            delivery.save()
            _delivery_event(delivery, "retry_scheduled", retry_in_seconds=delay)
            return "retry_scheduled"
        delivery.status = WorkflowEventDelivery.Status.FAILED
        delivery.completed_at = timezone.now()
        delivery.next_attempt_at = None
        delivery.save()
        _delivery_event(delivery, "failed", error=message[:500])
        transaction.on_commit(lambda: _on_delivery_finished(delivery.id))
    return "failed"


def apply_delivery_callback(delivery_id: Any, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    with transaction.atomic():
        delivery = (
            WorkflowEventDelivery.objects.select_for_update(of=("self",))
            .select_related("definition")
            .filter(id=delivery_id)
            .first()
        )
        if delivery is None:
            return 404, {"detail": "Delivery not found."}
        if delivery.status in (WorkflowEventDelivery.Status.COMPLETED, WorkflowEventDelivery.Status.FAILED):
            same = data["status"] == delivery.status
            return (200 if same else 409), {
                "detail": "Delivery is already finished.",
                "status": delivery.status,
            }
        if data.get("execution_id"):
            delivery.n8n_execution_id = data["execution_id"][:64]
        attempt = delivery.attempts
        if data["status"] == "completed":
            delivery.status = WorkflowEventDelivery.Status.COMPLETED
            delivery.result = {**(delivery.result or {}), **(data.get("summary") or {})}
            delivery.completed_at = timezone.now()
            delivery.deadline_at = None
            delivery.save()
            _delivery_event(delivery, "completed")
            transaction.on_commit(lambda: _on_delivery_finished(delivery.id))
            return 200, {"accepted": True, "status": delivery.status}
        delivery.save()
    error = data.get("error") or {}
    outcome = record_failed_delivery(
        delivery_id,
        attempt,
        str(error.get("message") or "The n8n workflow reported a failure."),
        retryable=bool(error.get("retryable", True)),
    )
    return 200, {"accepted": True, "outcome": outcome}


def _on_delivery_finished(delivery_id: Any) -> None:
    """Let the owning domain react (e.g. finish a knowledge integration sync)."""
    delivery = WorkflowEventDelivery.objects.filter(id=delivery_id).first()
    if delivery is not None and delivery.event_type == "knowledge.integration.sync_requested":
        from apps.orchestration.knowledge import finish_integration_sync

        finish_integration_sync(delivery)


def sweep_deliveries(now: Any = None) -> dict[str, int]:
    now = now or timezone.now()
    counts = {"delivered": 0, "timed_out": 0, "released": 0}
    # A worker that died mid-request leaves DELIVERING behind; its lease expiry frees it.
    counts["released"] = WorkflowEventDelivery.objects.filter(
        status=WorkflowEventDelivery.Status.DELIVERING, next_attempt_at__lt=now
    ).update(status=WorkflowEventDelivery.Status.PENDING)
    due = WorkflowEventDelivery.objects.filter(
        status=WorkflowEventDelivery.Status.PENDING, next_attempt_at__lte=now
    ).values_list("id", flat=True)[:200]
    for delivery_id in due:
        if deliver(delivery_id) in {"accepted", "retry_scheduled", "failed"}:
            counts["delivered"] += 1
    silent = WorkflowEventDelivery.objects.filter(
        status=WorkflowEventDelivery.Status.ACCEPTED, deadline_at__lt=now
    ).values_list("id", "attempts")[:200]
    for delivery_id, attempt in silent:
        record_failed_delivery(delivery_id, attempt, "The n8n execution sent no status before its timeout.")
        counts["timed_out"] += 1
    return counts


def fail_by_execution(execution_id: str, message: str) -> str | None:
    delivery = (
        WorkflowEventDelivery.objects.filter(
            n8n_execution_id=execution_id, status=WorkflowEventDelivery.Status.ACCEPTED
        )
        .only("id", "attempts")
        .first()
    )
    if delivery is None:
        return None
    return record_failed_delivery(delivery.id, delivery.attempts, message)


def open_deliveries_for(event_type: str, **payload_filters: Any) -> Any:
    query = WorkflowEventDelivery.objects.filter(event_type=event_type, status__in=_OPEN)
    for key, value in payload_filters.items():
        query = query.filter(**{f"payload__{key}": value})
    return query


__all__ = [
    "WorkflowDefinition",
    "apply_delivery_callback",
    "deliver",
    "fan_out",
    "open_deliveries_for",
    "record_failed_delivery",
    "sweep_deliveries",
]
