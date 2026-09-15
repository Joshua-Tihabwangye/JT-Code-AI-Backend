from celery import shared_task
from django.db import transaction
from django.utils import timezone
import sentry_sdk
from apps.events.kafka import publish
from apps.events.models import OutboxEvent

@shared_task
def publish_outbox_batch(limit: int = 100) -> int:
    published = 0
    with transaction.atomic():
        events = list(OutboxEvent.objects.select_for_update(skip_locked=True).filter(
            status=OutboxEvent.Status.PENDING, available_at__lte=timezone.now(),
        ).order_by('created_at')[:limit])
        for event in events:
            try:
                publish(event.topic, event.event_key, event.payload, event.headers)
                event.status = OutboxEvent.Status.PUBLISHED
                event.published_at = timezone.now()
                event.attempts += 1
                event.last_error = ''
                event.save(update_fields=('status', 'published_at', 'attempts', 'last_error'))
                published += 1
            except Exception as exc:
                event.attempts += 1
                event.last_error = str(exc)[:2000]
                if event.attempts >= 10:
                    event.status = OutboxEvent.Status.FAILED
                event.save(update_fields=('attempts', 'last_error', 'status'))
                sentry_sdk.capture_exception(exc)
    return published
