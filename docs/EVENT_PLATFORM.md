# Phase 6 event platform

Phase 6 uses Kafka as a durable integration-event bus. PostgreSQL remains the source of truth: application writes create an `OutboxEvent` in the same transaction, and the publisher sends it later. This setup does not require Docker.

## Contract

Every Kafka value is a JSON envelope with schema version `1`:

```json
{
  "event_id": "UUID",
  "event_type": "jobs.job.completed",
  "schema_version": 1,
  "occurred_at": "2026-09-30T12:00:00+00:00",
  "data": {},
  "request_id": "request correlation ID",
  "trace_id": "trace correlation ID",
  "causation_id": "optional upstream event ID"
}
```

`event_id`, `event_type`, `schema_version`, `request_id`, `trace_id`, and `causation_id` are also Kafka headers. Consumers must reject unknown schema versions. Domain payloads remain under `data`, preventing transport metadata from colliding with business fields.

Event types are lowercase `domain.entity[.action]` names (validated on write) and every topic is exactly `<KAFKA_TOPIC_PREFIX>.<event_type>`. Both `add_outbox_event` and the legacy `enqueue_outbox_event` normalize onto that prefixed topic, and the publisher re-derives it from the event type, so producers and `run_kafka_consumer` subscriptions always agree. `apps.events.contracts.KNOWN_EVENT_TYPES` is the registry of emitted events.

The originating request's `request_id`/`trace_id` are captured into the outbox row when it is written (the publisher runs in Celery, outside the request); worker-emitted events carry the entity's stored trace ID.

## Topic provisioning

Do not rely on broker auto-creation. Before the first deploy, and whenever `KNOWN_EVENT_TYPES` grows, run:

```bash
python manage.py ensure_kafka_topics --dry-run   # list missing topics
python manage.py ensure_kafka_topics             # create them
```

Topics are created with `KAFKA_TOPIC_PARTITIONS`, `KAFKA_TOPIC_REPLICATION_FACTOR`, and `KAFKA_TOPIC_RETENTION_MS`. The event key is the aggregate ID, so per-aggregate order holds within a partition; a retried outbox row can still be delivered after a later event for the same key, so consumers must tolerate reordering (use state checks, as the webhook handler does).

## Publishing and failure policy

Celery Beat runs `publish_outbox_batch` every two seconds (up to 500 events, produced as one batch and confirmed with a single flush). It claims a short, token-bound publisher lease in PostgreSQL, then sends to Kafka after the database transaction ends; a crashed worker's lease expires after `EVENT_OUTBOX_LEASE_SECONDS` and is safely reclaimed. This is deliberately **at-least-once** delivery: a crash after Kafka accepts the record but before the local confirmation can publish it again, so consumers must use the event ID idempotency ledger. A failed delivery remains pending, receives exponential backoff capped by `EVENT_OUTBOX_MAX_BACKOFF_SECONDS`, and is marked failed after `EVENT_OUTBOX_MAX_ATTEMPTS`. Failed outbox rows are retained for investigation; they are never silently discarded. Published rows are pruned hourly after `EVENT_OUTBOX_RETENTION_DAYS` (default 7).

## Consumers

Register a handler explicitly in `apps.events.consumers`:

```python
from apps.events.consumers import register_handler


@register_handler("billing.invoice.paid")
def handle_invoice_paid(envelope):
    # Make only Django database changes here.
    ...
```

The integrations domain registers `integrations.webhook.received`. External systems post to `POST /api/v1/inbound-webhooks/<webhook_id>/` with `X-Webhook-Timestamp: <unix seconds>` and `X-Webhook-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>`; timestamps older than five minutes and replays of the same signature are rejected. It verifies
that the envelope's delivery and webhook identifiers match the already verified
inbound delivery, then transitions that delivery from `pending` to `delivered`
in the same transaction as the `ConsumedEvent` idempotency record. It performs
no network request in the Kafka consumer.

Run a consumer against a managed Kafka service:

```bash
python manage.py run_kafka_consumer integrations.webhook.received --consumer-name integration-webhooks
```

The resulting group is `<KAFKA_CONSUMER_GROUP_PREFIX>.integration-webhooks`. Offsets are committed only after the handler and the durable `ConsumedEvent` idempotency record commit in the same database transaction. A duplicate event ID in the same group is acknowledged without calling the handler again.

Failure policy:

- Contract errors (bad envelope, header mismatch, no registered handler) are dead-lettered immediately.
- Any other handler error is retried in place up to `KAFKA_CONSUMER_MAX_ATTEMPTS` times with exponential backoff (capped by `KAFKA_CONSUMER_RETRY_MAX_SECONDS`), then dead-lettered.
- An offset-commit failure is not a message failure: the consumer exits without writing a dead letter and, after restart, the redelivered message is absorbed by `ConsumedEvent`.
- SIGTERM/SIGINT finish the current message, then close the consumer cleanly. Run it under a supervisor that restarts on exit.
- SASL settings are only sent when `KAFKA_SASL_MECHANISM` is set, so PLAINTEXT development brokers work.

## Dead letters and replay

Malformed, unsupported, or handler-failed events are stored in `DeadLetterEvent`, then a versioned `events.dead_lettered` outbox event is emitted in the same database transaction. The source Kafka offset is committed only after this database write succeeds, preventing poison-message loops without losing diagnostic data. A source topic/partition/offset can create only one durable dead letter.

Investigate `DeadLetterEvent` through Django admin. Replay is a deliberate operator action: correct the faulty handler or contract first, then run `python manage.py replay_dead_letter <uuid> --confirm --actor <operator>`. The command accepts only a valid version-1 envelope, publishes a new outbox event (therefore a new event ID), records the original as causation, and permits exactly one replay. Do not mutate a consumed message or delete its idempotency record to force a retry.

## Production drill

1. Create an outbox event and verify it is published with the version-1 envelope.
2. Deliver the same Kafka record twice to one consumer group; confirm one `ConsumedEvent` and one handler effect.
3. Deliver an invalid envelope; confirm one `DeadLetterEvent`, an `events.dead_lettered` outbox row, and a committed source offset.
4. Stop Kafka temporarily; confirm `OutboxEvent.available_at` moves forward with backoff and the event is not lost.

Run this drill against staging Kafka. The automated tests exercise these state transitions at the Kafka client boundary and intentionally do not require a Docker or broker process.
