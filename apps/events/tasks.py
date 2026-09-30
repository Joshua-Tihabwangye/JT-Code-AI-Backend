from datetime import timedelta
from uuid import UUID, uuid4

import sentry_sdk
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.events.contracts import build_envelope
from apps.events.kafka import publish
from apps.events.models import OutboxEvent
from apps.events.outbox import event_type


def _reclaim_expired_leases(now) -> None:
    """Return abandoned publisher leases to the outbox without losing them."""
    expires_before = now - timedelta(seconds=settings.EVENT_OUTBOX_LEASE_SECONDS)
    with transaction.atomic():
        stale_events = list(
            OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(
                status=OutboxEvent.Status.PUBLISHING,
                publishing_started_at__lt=expires_before,
            )
            .order_by("publishing_started_at")
        )
        for event in stale_events:
            event.publishing_started_at = None
            event.publishing_token = None
            event.last_error = "Publisher lease expired before delivery was confirmed."
            if event.attempts >= settings.EVENT_OUTBOX_MAX_ATTEMPTS:
                event.status = OutboxEvent.Status.FAILED
            else:
                event.status = OutboxEvent.Status.PENDING
                event.available_at = now
            event.save(
                update_fields=(
                    "status",
                    "available_at",
                    "publishing_started_at",
                    "publishing_token",
                    "last_error",
                )
            )


def _claim_due_events(limit: int) -> list[tuple[OutboxEvent, UUID]]:
    now = timezone.now()
    _reclaim_expired_leases(now)
    claimed: list[tuple[OutboxEvent, UUID]] = []
    with transaction.atomic():
        events = list(
            OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(
                status=OutboxEvent.Status.PENDING,
                available_at__lte=now,
                attempts__lt=settings.EVENT_OUTBOX_MAX_ATTEMPTS,
            )
            .order_by("created_at")[:limit]
        )
        for event in events:
            token = uuid4()
            event.status = OutboxEvent.Status.PUBLISHING
            event.publishing_started_at = now
            event.publishing_token = token
            event.attempts += 1
            event.save(update_fields=("status", "publishing_started_at", "publishing_token", "attempts"))
            claimed.append((event, token))
    return claimed


def _mark_published(event_id, token: UUID) -> bool:
    """Settle only the publisher lease that performed this delivery attempt."""
    return bool(
        OutboxEvent.objects.filter(
            id=event_id,
            status=OutboxEvent.Status.PUBLISHING,
            publishing_token=token,
        ).update(
            status=OutboxEvent.Status.PUBLISHED,
            publishing_started_at=None,
            publishing_token=None,
            published_at=timezone.now(),
            last_error="",
        )
    )


def _mark_delivery_failure(event_id, token: UUID, error: Exception) -> None:
    with transaction.atomic():
        event = (
            OutboxEvent.objects.select_for_update()
            .filter(
                id=event_id,
                status=OutboxEvent.Status.PUBLISHING,
                publishing_token=token,
            )
            .first()
        )
        if event is None:
            return
        event.publishing_started_at = None
        event.publishing_token = None
        event.last_error = str(error)[:2000]
        if event.attempts >= settings.EVENT_OUTBOX_MAX_ATTEMPTS:
            event.status = OutboxEvent.Status.FAILED
        else:
            event.status = OutboxEvent.Status.PENDING
            event.available_at = timezone.now() + timedelta(
                seconds=min(settings.EVENT_OUTBOX_MAX_BACKOFF_SECONDS, 2**event.attempts)
            )
        event.save(
            update_fields=(
                "status",
                "available_at",
                "publishing_started_at",
                "publishing_token",
                "last_error",
            )
        )


@shared_task
def publish_outbox_batch(limit: int = 100) -> int:
    """Publish claimed events outside database transactions (at-least-once delivery)."""
    published = 0
    for event, token in _claim_due_events(max(1, limit)):
        try:
            envelope = build_envelope(
                event_id=str(event.id),
                event_type=event_type(event),
                payload=event.payload,
                headers=event.headers,
            )
            publish(event.topic, event.event_key, envelope, event.headers)
            published += int(_mark_published(event.id, token))
        except Exception as exc:
            _mark_delivery_failure(event.id, token, exc)
            sentry_sdk.capture_exception(exc)
    return published
