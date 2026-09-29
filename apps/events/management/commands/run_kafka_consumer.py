from __future__ import annotations

import json
from typing import Any

import sentry_sdk
from confluent_kafka import Consumer, KafkaError
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.events.consumers import dead_letter_event, process_event
from apps.events.contracts import parse_envelope


class Command(BaseCommand):
    help = "Run an idempotent, manually committed JT-Code Kafka consumer."

    def add_arguments(self, parser) -> None:
        parser.add_argument("topics", nargs="+")
        parser.add_argument("--consumer-name", required=True)
        parser.add_argument("--group-id", help="Explicit group ID; must use the configured prefix.")

    @staticmethod
    def _headers(message) -> dict[str, str]:
        return {
            str(key): value.decode() if isinstance(value, bytes) else str(value or "")
            for key, value in (message.headers() or [])
        }

    @staticmethod
    def _payload(value: bytes | None) -> dict[str, Any]:
        try:
            parsed = json.loads((value or b"{}").decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"raw": (value or b"").decode(errors="replace")}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}

    def handle(self, *args, **options) -> None:
        consumer_name = options["consumer_name"]
        group_id = options["group_id"] or f"{settings.KAFKA_CONSUMER_GROUP_PREFIX}.{consumer_name}"
        if not group_id.startswith(f"{settings.KAFKA_CONSUMER_GROUP_PREFIX}."):
            raise CommandError("Kafka consumer group ID must start with KAFKA_CONSUMER_GROUP_PREFIX.")
        topics = [
            topic
            if topic.startswith(f"{settings.KAFKA_TOPIC_PREFIX}.")
            else f"{settings.KAFKA_TOPIC_PREFIX}.{topic}"
            for topic in options["topics"]
        ]
        consumer = Consumer(
            {
                "bootstrap.servers": settings.KAFKA_BOOTSTRAP_SERVERS,
                "group.id": group_id,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
                "sasl.mechanism": settings.KAFKA_SASL_MECHANISM,
                "sasl.username": settings.KAFKA_SASL_USERNAME,
                "sasl.password": settings.KAFKA_SASL_PASSWORD,
                "client.id": f"{settings.KAFKA_CLIENT_ID}-{consumer_name}",
                "security.protocol": settings.KAFKA_SECURITY_PROTOCOL,
            }
        )
        consumer.subscribe(topics)
        self.stdout.write(f"Consuming {topics} as {group_id}")
        try:
            while True:
                message = consumer.poll(1.0)
                if message is None:
                    continue
                if message.error():
                    if message.error().code() != KafkaError._PARTITION_EOF:
                        raise RuntimeError(str(message.error()))
                    continue
                payload = self._payload(message.value())
                headers = self._headers(message)
                try:
                    envelope = parse_envelope(payload)
                    processed = process_event(
                        consumer_group=group_id,
                        envelope=envelope,
                        topic=message.topic(),
                        partition=message.partition(),
                        offset=message.offset(),
                    )
                    consumer.commit(message=message, asynchronous=False)
                    disposition = "processed" if processed else "duplicate"
                    self.stdout.write(f"{disposition} {message.topic()} {envelope.event_id}")
                except Exception as exc:  # A DLQ write is required before committing this offset.
                    sentry_sdk.capture_exception(exc)
                    dead_letter_event(
                        consumer_group=group_id,
                        topic=message.topic(),
                        payload=payload,
                        headers=headers,
                        partition=message.partition(),
                        offset=message.offset(),
                        error=str(exc),
                    )
                    consumer.commit(message=message, asynchronous=False)
                    self.stderr.write(f"dead-lettered {message.topic()} offset={message.offset()}: {exc}")
        finally:
            consumer.close()
