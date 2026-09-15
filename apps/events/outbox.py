from django.conf import settings
from apps.events.models import OutboxEvent

def topic_name(event_name: str) -> str:
    return f'{settings.KAFKA_TOPIC_PREFIX}.{event_name}'

def add_outbox_event(event_name: str, event_key: str, payload: dict, headers: dict | None = None) -> OutboxEvent:
    return OutboxEvent.objects.create(
        topic=topic_name(event_name), event_key=event_key, payload=payload, headers=headers or {},
    )

def enqueue_outbox_event(topic: str, event_key: str, payload: dict, headers: dict | None = None) -> OutboxEvent:
    return OutboxEvent.objects.create(
        topic=topic, event_key=event_key, payload=payload, headers=headers or {},
    )
