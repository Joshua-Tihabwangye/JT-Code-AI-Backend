from __future__ import annotations

import json
import logging
import os
import signal
import time
from typing import Any

import sentry_sdk
from confluent_kafka import Consumer, KafkaError
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connection

from apps.core.tracing import span_from_headers
from apps.events.consumers import UnhandledEventError, dead_letter_event, process_event
from apps.events.contracts import EventContractError, parse_envelope, validate_transport_headers
from apps.events.kafka import kafka_client_config

logger = logging.getLogger(__name__)

# Errors that can never succeed on redelivery; everything else is retried first.
_PERMANENT_ERRORS = (EventContractError, UnhandledEventError)
_LAG_INTERVAL_SECONDS = 30
_HEARTBEAT_INTERVAL_SECONDS = 10


class Command(BaseCommand):
    help = "Run an idempotent, manually committed JT-Code Kafka consumer."

    def add_arguments(self, parser) -> None:
        parser.add_argument("topics", nargs="+")
        parser.add_argument("--consumer-name", required=True)
        parser.add_argument("--group-id", help="Explicit group ID; must use the configured prefix.")
        parser.add_argument(
            "--metrics-port",
            type=int,
            default=None,
            help="Serve Prometheus metrics on this port (default: CELERY_METRICS_PORT; 0 disables).",
        )
        parser.add_argument(
            "--heartbeat-file",
            default="/tmp/kafka-consumer.heartbeat",  # nosec B108 - per-pod emptyDir, read by the liveness probe
            help="Touched while the poll loop runs; the liveness probe checks its age.",
        )

    @staticmethod
    def _record_lag(consumer, group_id: str) -> None:
        """Publish messages-behind per assigned partition (high watermark minus committed offset)."""
        from apps.core.metrics import KAFKA_CONSUMER_LAG

        assignment = consumer.assignment()
        if not assignment:
            return
        for tp in consumer.committed(assignment, timeout=5):
            low, high = consumer.get_watermark_offsets(tp, timeout=2, cached=False)
            # A negative offset means nothing is committed yet: everything retained is unconsumed.
            lag = high - (tp.offset if tp.offset >= 0 else low)
            KAFKA_CONSUMER_LAG.labels(group_id, tp.topic, str(tp.partition)).set(max(lag, 0))

    @staticmethod
    def _touch(path: str) -> None:
        with open(path, "a"):
            os.utime(path, None)

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
        except UnicodeDecodeError, json.JSONDecodeError:
            return {"raw": (value or b"").decode(errors="replace")}
        return parsed if isinstance(parsed, dict) else {"raw": parsed}

    def _process_with_retry(self, *, group_id: str, message, payload, headers) -> str:
        """Return ``processed``/``duplicate``, or raise the final error to dead-letter."""
        envelope = parse_envelope(payload)
        validate_transport_headers(envelope, headers)
        attributes = {
            "messaging.system": "kafka",
            "messaging.destination.name": message.topic(),
            "messaging.consumer.group.name": group_id,
            "messaging.message.id": envelope.event_id,
        }
        # Continue the producer's trace (``traceparent`` captured by the outbox).
        with span_from_headers(f"consume {envelope.event_type}", headers, **attributes):
            return self._process_attempts(group_id=group_id, message=message, envelope=envelope)

    def _process_attempts(self, *, group_id: str, message, envelope) -> str:
        attempts = max(1, settings.KAFKA_CONSUMER_MAX_ATTEMPTS)
        for attempt in range(1, attempts + 1):
            try:
                processed = process_event(
                    consumer_group=group_id,
                    envelope=envelope,
                    topic=message.topic(),
                    partition=message.partition(),
                    offset=message.offset(),
                )
                return "processed" if processed else "duplicate"
            except _PERMANENT_ERRORS:
                raise
            except Exception as exc:  # noqa: BLE001 - transient failures are retried in place
                if attempt == attempts:
                    raise
                logger.warning(
                    "kafka handler attempt failed; retrying",
                    extra={"dependency": "kafka", "checks": {"attempt": attempt, "error": str(exc)[:200]}},
                )
                if not connection.in_atomic_block:
                    connection.close()  # discard a possibly broken DB connection before retrying
                time.sleep(min(settings.KAFKA_CONSUMER_RETRY_MAX_SECONDS, 2 ** (attempt - 1)))
        raise RuntimeError("unreachable")

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
            kafka_client_config(
                **{
                    "group.id": group_id,
                    "client.id": f"{settings.KAFKA_CLIENT_ID}-{consumer_name}",
                    "auto.offset.reset": "earliest",
                    "enable.auto.commit": False,
                    "enable.auto.offset.store": False,
                }
            )
        )
        stopping = False

        def request_stop(signum, _frame) -> None:
            nonlocal stopping
            stopping = True
            self.stdout.write(f"Received signal {signum}; finishing the current message.")

        previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        from apps.core.metrics import KAFKA_CONSUMED, KAFKA_CONSUMER_LAST_POLL, start_metrics_server

        metrics_port = options["metrics_port"]
        start_metrics_server(settings.CELERY_METRICS_PORT if metrics_port is None else metrics_port)
        heartbeat = options["heartbeat_file"]
        consumer.subscribe(topics)
        self.stdout.write(f"Consuming {topics} as {group_id}")
        last_lag = last_beat = 0.0
        try:
            while not stopping:
                message = consumer.poll(1.0)
                now = time.time()
                KAFKA_CONSUMER_LAST_POLL.labels(group_id).set(now)
                if heartbeat and now - last_beat >= _HEARTBEAT_INTERVAL_SECONDS:
                    self._touch(heartbeat)
                    last_beat = now
                if now - last_lag >= _LAG_INTERVAL_SECONDS:
                    last_lag = now
                    try:
                        self._record_lag(consumer, group_id)
                    except Exception:  # noqa: BLE001 - lag is telemetry; never stop consuming over it
                        logger.warning("kafka consumer lag unavailable", extra={"dependency": "kafka"})
                if message is None:
                    continue
                if error := message.error():
                    if error.code() == KafkaError._PARTITION_EOF:
                        continue
                    if error.fatal():
                        raise RuntimeError(str(error))
                    logger.warning("kafka consumer error", extra={"dependency": "kafka"})
                    continue
                if not connection.in_atomic_block:
                    close_old_connections()
                payload = self._payload(message.value())
                headers = self._headers(message)
                try:
                    disposition = self._process_with_retry(
                        group_id=group_id, message=message, payload=payload, headers=headers
                    )
                except Exception as exc:  # noqa: BLE001 - dead-letter before committing the offset
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
                    disposition = "dead-lettered"
                # A commit failure is not a message failure: let it propagate so the
                # consumer restarts; redelivery is absorbed by ConsumedEvent/DLQ idempotency.
                consumer.commit(message=message, asynchronous=False)
                KAFKA_CONSUMED.labels(group_id, message.topic(), disposition).inc()
                self.stdout.write(f"{disposition} {message.topic()} offset={message.offset()}")
        finally:
            consumer.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
