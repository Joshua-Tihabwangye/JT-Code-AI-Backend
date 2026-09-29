from datetime import timedelta

import sentry_sdk
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.events.contracts import build_envelope
from apps.events.kafka import publish
from apps.events.models import OutboxEvent
from apps.events.outbox import event_type


@shared_task
def publish_outbox_batch(limit: int = 100) -> int:
    published = 0
    with transaction.atomic():
        events = list(
            OutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(
                status=OutboxEvent.Status.PENDING,
                available_at__lte=timezone.now(),
            )
            .order_by("created_at")[:limit]
        )
        for event in events:
            try:
                envelope = build_envelope(
                    event_id=str(event.id),
                    event_type=event_type(event),
                    payload=event.payload,
                    headers=event.headers,
                )
                publish(event.topic, event.event_key, envelope, event.headers)
                event.status = OutboxEvent.Status.PUBLISHED
                event.published_at = timezone.now()
                event.attempts += 1
                event.last_error = ""
                event.save(update_fields=("status", "published_at", "attempts", "last_error", "available_at"))
                published += 1
            except Exception as exc:
                event.attempts += 1
                event.last_error = str(exc)[:2000]
                if event.attempts >= settings.EVENT_OUTBOX_MAX_ATTEMPTS:
                    event.status = OutboxEvent.Status.FAILED
                else:
                    event.available_at = timezone.now() + timedelta(
                        seconds=min(settings.EVENT_OUTBOX_MAX_BACKOFF_SECONDS, 2**event.attempts)
                    )
                event.save(update_fields=("attempts", "last_error", "status", "available_at"))
                sentry_sdk.capture_exception(exc)
    return published
