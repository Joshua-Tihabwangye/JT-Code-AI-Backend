"""Transactional outbox helpers for versioned domain events."""

from __future__ import annotations

from django.conf import settings

from apps.events.contracts import current_correlation_headers, event_type_for_topic, validate_event_type
from apps.events.models import OutboxEvent


def topic_name(event_name: str) -> str:
    return f"{settings.KAFKA_TOPIC_PREFIX}.{event_name}"


def _headers_with_correlation(headers: dict | None) -> dict:
    """Capture the originating request's IDs now; the publisher runs in another context."""
    return {**current_correlation_headers(), **{k: v for k, v in (headers or {}).items() if v}}


def add_outbox_event(
    event_name: str, event_key: str, payload: dict, headers: dict | None = None
) -> OutboxEvent:
    """Record an event in the caller's transaction on ``<prefix>.<event_name>``."""
    return OutboxEvent.objects.create(
        topic=topic_name(validate_event_type(event_name)),
        event_key=event_key,
        payload=payload,
        headers=_headers_with_correlation(headers),
    )


def enqueue_outbox_event(
    topic: str, event_key: str, payload: dict, headers: dict | None = None
) -> OutboxEvent:
    """Backward-compatible alias accepting an event type or an already-prefixed topic.

    Every event is normalized onto the prefixed topic so producers and
    ``run_kafka_consumer`` subscriptions always agree.
    """
    return add_outbox_event(
        event_type_for_topic(topic, settings.KAFKA_TOPIC_PREFIX), event_key, payload, headers
    )


def event_type(event: OutboxEvent) -> str:
    """Return the stable contract name for an outbox event topic."""
    return event_type_for_topic(event.topic, settings.KAFKA_TOPIC_PREFIX)
