from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from confluent_kafka import Producer
from django.conf import settings

from apps.events.contracts import EventEnvelope, transport_headers


def kafka_client_config(**overrides: Any) -> dict[str, Any]:
    """Return the connection/security settings shared by every Kafka client.

    SASL properties are only included when a mechanism is configured: librdkafka
    rejects an empty ``sasl.mechanism``, which would stop PLAINTEXT development
    brokers from working.
    """
    config: dict[str, Any] = {
        "bootstrap.servers": settings.KAFKA_BOOTSTRAP_SERVERS,
        "client.id": settings.KAFKA_CLIENT_ID,
    }
    if settings.KAFKA_SECURITY_PROTOCOL:
        config["security.protocol"] = settings.KAFKA_SECURITY_PROTOCOL
    if settings.KAFKA_SASL_MECHANISM:
        config.update(
            {
                "sasl.mechanism": settings.KAFKA_SASL_MECHANISM,
                "sasl.username": settings.KAFKA_SASL_USERNAME,
                "sasl.password": settings.KAFKA_SASL_PASSWORD,
            }
        )
    config.update(overrides)
    return config


@lru_cache(maxsize=1)
def producer() -> Producer:
    return Producer(
        kafka_client_config(
            **{
                "enable.idempotence": True,
                "acks": "all",
                "compression.type": "snappy",
                "linger.ms": 5,
                "delivery.timeout.ms": 10000,
                "request.timeout.ms": 5000,
            }
        )
    )


def _encode(envelope: EventEnvelope, headers: dict[str, str] | None) -> tuple[bytes, list[tuple[str, bytes]]]:
    value = json.dumps(envelope.as_dict(), separators=(",", ":"), default=str).encode()
    return value, [(name, value.encode()) for name, value in transport_headers(envelope, headers).items()]


def publish_many(
    records: list[tuple[str, str, EventEnvelope, dict[str, str] | None]], *, timeout: float = 10.0
) -> dict[str, str | None]:
    """Produce a batch and wait once; return ``{event_id: error or None}``.

    A missing result means the record was not confirmed before ``timeout`` and
    must be treated as a failed attempt (it may still arrive: at-least-once).
    """
    p = producer()
    results: dict[str, str | None] = {}

    for topic, key, envelope, headers in records:
        event_id = envelope.event_id

        def on_delivery(error, _message, event_id: str = event_id) -> None:
            results[event_id] = None if error is None else str(error)

        value, encoded_headers = _encode(envelope, headers)
        try:
            p.produce(
                topic=topic, key=key.encode(), value=value, headers=encoded_headers, on_delivery=on_delivery
            )
        except BufferError:
            p.poll(1)
            p.produce(
                topic=topic, key=key.encode(), value=value, headers=encoded_headers, on_delivery=on_delivery
            )
        p.poll(0)
    p.flush(timeout)
    for _topic, _key, envelope, _headers in records:
        results.setdefault(envelope.event_id, "Kafka delivery was not confirmed before timeout.")
    return results


def publish(topic: str, key: str, envelope: EventEnvelope, headers: dict[str, str] | None = None) -> None:
    error = publish_many([(topic, key, envelope, headers)])[envelope.event_id]
    if error:
        raise RuntimeError(f"Kafka delivery failed: {error}")
