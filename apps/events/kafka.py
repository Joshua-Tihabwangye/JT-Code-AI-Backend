from __future__ import annotations

import json
from functools import lru_cache
from confluent_kafka import Producer
from django.conf import settings

@lru_cache(maxsize=1)
def producer() -> Producer:
    config = {
        'bootstrap.servers': settings.KAFKA_BOOTSTRAP_SERVERS,
        'client.id': settings.KAFKA_CLIENT_ID,
        'security.protocol': settings.KAFKA_SECURITY_PROTOCOL,
        'enable.idempotence': True,
        'acks': 'all',
        'compression.type': 'snappy',
    }
    if settings.KAFKA_SASL_MECHANISM:
        config.update({
            'sasl.mechanism': settings.KAFKA_SASL_MECHANISM,
            'sasl.username': settings.KAFKA_SASL_USERNAME,
            'sasl.password': settings.KAFKA_SASL_PASSWORD,
        })
    return Producer(config)

def publish(topic: str, key: str, payload: dict, headers: dict[str, str] | None = None) -> None:
    p = producer()
    p.produce(
        topic=topic,
        key=key.encode(),
        value=json.dumps(payload, separators=(',', ':'), default=str).encode(),
        headers=[(name, value.encode()) for name, value in (headers or {}).items()],
    )
    remaining = p.flush(10)
    if remaining:
        raise TimeoutError(f'{remaining} Kafka message(s) were not delivered before timeout.')
