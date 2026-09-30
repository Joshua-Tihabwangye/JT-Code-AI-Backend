from __future__ import annotations

import json
from functools import lru_cache

from confluent_kafka import Producer
from django.conf import settings

from apps.events.contracts import EventEnvelope, transport_headers


@lru_cache(maxsize=1)
def producer() -> Producer:
    config = {
        "bootstrap.servers": settings.KAFKA_BOOTSTRAP_SERVERS,
        "client.id": settings.KAFKA_CLIENT_ID,
        "security.protocol": settings.KAFKA_SECURITY_PROTOCOL,
        "enable.idempotence": True,
        "acks": "all",
        "compression.type": "snappy",
        "delivery.timeout.ms": 10000,
        "request.timeout.ms": 5000,
    }
    if settings.KAFKA_SASL_MECHANISM:
        config.update(
            {
                "sasl.mechanism": settings.KAFKA_SASL_MECHANISM,
                "sasl.username": settings.KAFKA_SASL_USERNAME,
                "sasl.password": settings.KAFKA_SASL_PASSWORD,
            }
        )
    return Producer(config)


def publish(topic: str, key: str, envelope: EventEnvelope, headers: dict[str, str] | None = None) -> None:
    p = producer()
    delivery_errors: list[str] = []

    def on_delivery(error, _message) -> None:
        if error is not None:
            delivery_errors.append(str(error))

    p.produce(
        topic=topic,
        key=key.encode(),
        value=json.dumps(envelope.as_dict(), separators=(",", ":"), default=str).encode(),
        headers=[(name, value.encode()) for name, value in transport_headers(envelope, headers).items()],
        on_delivery=on_delivery,
    )
    remaining = p.flush(10)
    if remaining:
        raise TimeoutError(f"{remaining} Kafka message(s) were not delivered before timeout.")
    if delivery_errors:
        raise RuntimeError(f"Kafka delivery failed: {delivery_errors[0]}")
