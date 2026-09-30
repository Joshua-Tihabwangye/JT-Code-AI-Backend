"""Idempotent domain-event consumer registry and dead-letter persistence."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.db import IntegrityError, transaction

from apps.events.contracts import EventEnvelope, parse_envelope
from apps.events.models import ConsumedEvent, DeadLetterEvent
from apps.events.outbox import add_outbox_event

EventHandler = Callable[[EventEnvelope], None]
_HANDLERS: dict[str, EventHandler] = {}


class UnhandledEventError(RuntimeError):
    """A consumer subscribed to an event without registering a handler."""


def register_handler(event_type: str) -> Callable[[EventHandler], EventHandler]:
    def decorate(handler: EventHandler) -> EventHandler:
        if event_type in _HANDLERS:
            raise RuntimeError(f"A handler is already registered for {event_type!r}.")
        _HANDLERS[event_type] = handler
        return handler

    return decorate


def registered_event_types() -> tuple[str, ...]:
    return tuple(sorted(_HANDLERS))


def process_event(
    *,
    consumer_group: str,
    envelope: EventEnvelope,
    topic: str,
    partition: int,
    offset: int,
) -> bool:
    """Run a handler once and record success in the same database transaction.

    Returns ``False`` for a duplicate delivery. The Kafka offset must only be
    committed after this function returns successfully.
    """
    handler = _HANDLERS.get(envelope.event_type)
    if handler is None:
        raise UnhandledEventError(f"No handler registered for {envelope.event_type!r}.")
    try:
        with transaction.atomic():
            # Insert the unique idempotency key before invoking the handler.
            # A competing delivery blocks on this constraint and then returns
            # duplicate after the first transaction commits.
            ConsumedEvent.objects.create(
                consumer_group=consumer_group,
                event_id=envelope.event_id,
                event_type=envelope.event_type,
                topic=topic,
                partition=partition,
                offset=offset,
            )
            handler(envelope)
    except IntegrityError:
        return False
    return True


def dead_letter_event(
    *,
    consumer_group: str,
    topic: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    error: str,
    partition: int | None = None,
    offset: int | None = None,
) -> DeadLetterEvent:
    """Persist a rejected event and publish a versioned DLQ notification via outbox."""
    with transaction.atomic():
        if partition is not None and offset is not None:
            dead_letter, created = DeadLetterEvent.objects.get_or_create(
                consumer_group=consumer_group,
                topic=topic,
                partition=partition,
                offset=offset,
                defaults={
                    "event_id": str(payload.get("event_id") or ""),
                    "event_type": str(payload.get("event_type") or ""),
                    "payload": payload,
                    "headers": headers,
                    "error": error[:2000],
                },
            )
        else:
            dead_letter = DeadLetterEvent.objects.create(
                consumer_group=consumer_group,
                event_id=str(payload.get("event_id") or ""),
                event_type=str(payload.get("event_type") or ""),
                topic=topic,
                partition=partition,
                offset=offset,
                payload=payload,
                headers=headers,
                error=error[:2000],
            )
            created = True
        if created:
            add_outbox_event(
                event_name="events.dead_lettered",
                event_key=str(dead_letter.id),
                payload={
                    "dead_letter_id": str(dead_letter.id),
                    "consumer_group": consumer_group,
                    "source_topic": topic,
                    "event_id": dead_letter.event_id,
                    "event_type": dead_letter.event_type,
                    "error": dead_letter.error,
                },
                headers={
                    "request_id": headers.get("request_id", ""),
                    "trace_id": headers.get("trace_id", ""),
                    "causation_id": dead_letter.event_id,
                },
            )
    return dead_letter


def replay_dead_letter(*, dead_letter_id, actor: str = "") -> DeadLetterEvent:
    """Create a new outbox event from a valid DLQ envelope after operator review."""
    from django.utils import timezone

    with transaction.atomic():
        dead_letter = DeadLetterEvent.objects.select_for_update().get(id=dead_letter_id)
        if dead_letter.replayed_at is not None:
            raise ValueError("This dead-letter event has already been replayed.")
        envelope = parse_envelope(dead_letter.payload)
        add_outbox_event(
            event_name=envelope.event_type,
            event_key=envelope.event_id,
            payload=envelope.data,
            headers={
                **dead_letter.headers,
                "request_id": envelope.request_id,
                "trace_id": envelope.trace_id,
                "causation_id": envelope.event_id,
                "replayed_by": actor,
            },
        )
        dead_letter.replayed_at = timezone.now()
        dead_letter.save(update_fields=("replayed_at",))
    return dead_letter
