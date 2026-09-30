from datetime import timedelta
from uuid import UUID, uuid4

import sentry_sdk
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.events.contracts import build_envelope
from apps.events.kafka import publish_many
from apps.events.models import OutboxEvent
from apps.events.outbox import event_type, topic_name


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
def publish_outbox_batch(limit: int = 500) -> int:
    """Publish claimed events outside database transactions (at-least-once delivery).

    The batch is produced together and confirmed with one flush; each event is
    then settled individually against its own publisher lease.
    """
    claimed = _claim_due_events(max(1, limit))
    records = []
    tokens: dict[str, tuple[OutboxEvent, UUID]] = {}
    for event, token in claimed:
        try:
            name = event_type(event)
            envelope = build_envelope(
                event_id=str(event.id),
                event_type=name,
                payload=event.payload,
                headers=event.headers,
            )
        except Exception as exc:  # noqa: BLE001 - one malformed row must not block the batch
            _mark_delivery_failure(event.id, token, exc)
            sentry_sdk.capture_exception(exc)
            continue
        tokens[envelope.event_id] = (event, token)
        records.append((topic_name(name), event.event_key, envelope, event.headers))
    if not records:
        return 0
    try:
        results = publish_many(records)
    except Exception as exc:  # noqa: BLE001 - producer construction/transport failure
        sentry_sdk.capture_exception(exc)
        results = {event_id: str(exc) for event_id in tokens}
    published = 0
    for event_id, (event, token) in tokens.items():
        if (error := results.get(event_id)) is None:
            published += int(_mark_published(event.id, token))
        else:
            _mark_delivery_failure(event.id, token, RuntimeError(error))
    return published


@shared_task
def prune_published_outbox_events() -> int:
    """Delete confirmed outbox rows after the retention window; failed rows are kept."""
    cutoff = timezone.now() - timedelta(days=settings.EVENT_OUTBOX_RETENTION_DAYS)
    deleted, _ = OutboxEvent.objects.filter(
        status=OutboxEvent.Status.PUBLISHED, published_at__lt=cutoff
    ).delete()
    return deleted
