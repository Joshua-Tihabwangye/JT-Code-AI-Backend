"""Recovery and saturation drills (Phase 18), run against real infrastructure.

* **DLQ drill** - a poison event fails every consumer attempt, is dead-lettered
  (``DeadLetterEvent`` + ``events.dead_lettered``), the fault is fixed, the
  event is replayed by an operator and consumed exactly once. Uses the same
  ``process_event`` / ``dead_letter_event`` / ``replay_dead_letter`` code paths
  as ``run_kafka_consumer``.
* **Saturation drill** - floods Celery queues with no-op tasks (and optionally
  Kafka with drill events) and measures dispatch rate, drain throughput and
  end-to-end latency, the inputs of the capacity plan.
"""

from __future__ import annotations

import math
import time
import uuid
from typing import Any

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from apps.events.consumers import (
    dead_letter_event,
    process_event,
    register_handler,
    replay_dead_letter,
)
from apps.events.contracts import EventEnvelope, build_envelope

POISON_EVENT = "operations.drill.poison"
_FIXED_KEY = "ops-drill-fixed:{}"
LATENCY_BUCKETS_MS = (25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000, 300000)


class DrillFailure(RuntimeError):
    """The drill's poison handler refusing an event that is not yet "fixed"."""


@register_handler(POISON_EVENT)
def _poison_handler(envelope: EventEnvelope) -> None:
    drill_id = str(envelope.data.get("drill_id") or "")
    if not cache.get(_FIXED_KEY.format(drill_id)):
        raise DrillFailure(f"drill {drill_id}: handler deliberately failing (poison event)")


def run_dlq_drill(*, consumer_group: str = "jt-code.operations-drill") -> dict[str, Any]:
    from apps.events.models import ConsumedEvent, DeadLetterEvent, OutboxEvent

    drill_id = str(uuid.uuid4())
    started = time.monotonic()
    envelope = build_envelope(
        event_id=str(uuid.uuid4()),
        event_type=POISON_EVENT,
        payload={"drill_id": drill_id},
        headers={"trace_id": f"dlq-drill-{drill_id}"},
    )
    topic = f"{settings.KAFKA_TOPIC_PREFIX}.{POISON_EVENT}"
    attempts = max(1, settings.KAFKA_CONSUMER_MAX_ATTEMPTS)
    errors = []
    # 1. Every attempt fails, exactly like the consumer's in-place retries.
    for attempt in range(attempts):
        try:
            process_event(
                consumer_group=consumer_group, envelope=envelope, topic=topic, partition=0, offset=attempt
            )
        except DrillFailure as exc:
            errors.append(str(exc))
    if len(errors) != attempts:
        raise RuntimeError("DLQ drill: the poison event must fail every attempt")
    # 2. Dead-letter it.
    dead_letter = dead_letter_event(
        consumer_group=consumer_group,
        topic=topic,
        payload=envelope.as_dict(),
        headers={"trace_id": envelope.trace_id},
        error=errors[-1],
        partition=0,
        offset=attempts,
    )
    notified = OutboxEvent.objects.filter(
        topic__endswith="events.dead_lettered", payload__dead_letter_id=str(dead_letter.id)
    ).exists()
    # 3. Fix the fault, replay through the outbox, consume the replayed event once.
    cache.set(_FIXED_KEY.format(drill_id), "1", timeout=3600)
    replay_dead_letter(dead_letter_id=dead_letter.id, actor="dlq-drill")
    replayed = (
        OutboxEvent.objects.filter(topic=topic, payload__drill_id=drill_id).order_by("-created_at").first()
    )
    if replayed is None:
        raise RuntimeError("DLQ drill: replay must enqueue a new outbox event")
    replay_envelope = build_envelope(
        event_id=str(replayed.id), event_type=POISON_EVENT, payload=replayed.payload, headers=replayed.headers
    )
    first = process_event(
        consumer_group=consumer_group, envelope=replay_envelope, topic=topic, partition=0, offset=attempts + 1
    )
    duplicate = process_event(
        consumer_group=consumer_group, envelope=replay_envelope, topic=topic, partition=0, offset=attempts + 2
    )
    try:
        replay_dead_letter(dead_letter_id=dead_letter.id, actor="dlq-drill")
        double_replay_blocked = False
    except ValueError:
        double_replay_blocked = True
    with transaction.atomic():
        replayed.delete()  # the drill topic has no real consumer; keep the outbox clean
    return {
        "drillId": drill_id,
        "attemptsBeforeDeadLetter": attempts,
        "deadLetterId": str(dead_letter.id),
        "deadLetterNotified": notified,
        "replayedConsumed": first,
        "duplicateIgnored": duplicate is False,
        "doubleReplayBlocked": double_replay_blocked,
        "consumedOnce": ConsumedEvent.objects.filter(
            consumer_group=consumer_group, event_id=replay_envelope.event_id
        ).count()
        == 1,
        "markedReplayed": DeadLetterEvent.objects.filter(
            id=dead_letter.id, replayed_at__isnull=False
        ).exists(),
        "recoverySeconds": round(time.monotonic() - started, 3),
    }


# Saturation ---------------------------------------------------------------------------


def _bucket(latency_ms: float) -> str:
    for bound in LATENCY_BUCKETS_MS:
        if latency_ms <= bound:
            return str(bound)
    return "inf"


@shared_task(acks_late=True)  # type: ignore[untyped-decorator]
def drill_ping(run_id: str, queue: str, sent_at: float) -> None:
    """No-op work item; records its end-to-end latency in the shared cache (Redis)."""
    latency_ms = max(0.0, (time.time() - sent_at) * 1000)
    prefix = f"ops-sat:{run_id}:{queue}"
    cache.add(f"{prefix}:done", 0, timeout=7200)
    cache.incr(f"{prefix}:done")
    bucket = f"{prefix}:bucket:{_bucket(latency_ms)}"
    cache.add(bucket, 0, timeout=7200)
    cache.incr(bucket)
    cache.add(f"{prefix}:first", time.time(), timeout=7200)
    cache.set(f"{prefix}:last", time.time(), timeout=7200)


def percentile_from_buckets(counts: dict[str, int], quantile: float) -> float | None:
    total = sum(counts.values())
    if not total:
        return None
    target = math.ceil(total * quantile)
    running = 0
    for bound in [*map(str, LATENCY_BUCKETS_MS), "inf"]:
        running += counts.get(bound, 0)
        if running >= target:
            return float("inf") if bound == "inf" else float(bound)
    return None


def run_celery_saturation(*, queues: list[str], tasks_per_queue: int, timeout: float) -> dict[str, Any]:
    run_id = uuid.uuid4().hex[:12]
    report: dict[str, Any] = {"runId": run_id, "queues": {}}
    dispatch_started = time.time()
    for queue in queues:
        for _ in range(tasks_per_queue):
            drill_ping.apply_async(args=[run_id, queue, time.time()], queue=queue)
    dispatch_seconds = max(time.time() - dispatch_started, 1e-6)
    report["dispatchRatePerSecond"] = round(len(queues) * tasks_per_queue / dispatch_seconds, 1)
    deadline = time.time() + timeout
    for queue in queues:
        prefix = f"ops-sat:{run_id}:{queue}"
        while time.time() < deadline and int(cache.get(f"{prefix}:done") or 0) < tasks_per_queue:
            time.sleep(0.5)
        done = int(cache.get(f"{prefix}:done") or 0)
        counts = {str(b): int(cache.get(f"{prefix}:bucket:{b}") or 0) for b in [*LATENCY_BUCKETS_MS, "inf"]}
        first = float(cache.get(f"{prefix}:first") or dispatch_started)
        last = float(cache.get(f"{prefix}:last") or first)
        drain_seconds = max(last - dispatch_started, 1e-6)
        report["queues"][queue] = {
            "sent": tasks_per_queue,
            "completed": done,
            "drainSeconds": round(drain_seconds, 3),
            "throughputPerSecond": round(done / drain_seconds, 1),
            "latencyP50Ms": percentile_from_buckets(counts, 0.50),
            "latencyP95Ms": percentile_from_buckets(counts, 0.95),
            "latencyP99Ms": percentile_from_buckets(counts, 0.99),
        }
    return report


def run_kafka_saturation(*, events: int, timeout: float) -> dict[str, Any]:
    """Produce drill events and consume them with a throwaway consumer group."""
    from confluent_kafka import Consumer

    from apps.events.kafka import kafka_client_config, publish_many

    topic = f"{settings.KAFKA_TOPIC_PREFIX}.operations.drill.ping"
    group = f"{settings.KAFKA_CONSUMER_GROUP_PREFIX}.saturation-{uuid.uuid4().hex[:8]}"
    consumer = Consumer(
        kafka_client_config(**{"group.id": group, "auto.offset.reset": "latest", "enable.auto.commit": False})
    )
    consumer.subscribe([topic])
    consumer.poll(5)  # join the group before producing so "latest" includes our events
    records: list[tuple[str, str, EventEnvelope, dict[str, str] | None]] = []
    for index in range(events):
        envelope = build_envelope(
            event_id=str(uuid.uuid4()),
            event_type="operations.drill.ping",
            payload={"index": index, "sent_at": time.time()},
        )
        records.append((topic, str(index), envelope, {}))
    produce_started = time.time()
    results = publish_many(records, timeout=timeout)
    produce_seconds = max(time.time() - produce_started, 1e-6)
    failed = sum(1 for error in results.values() if error)
    latencies: list[float] = []
    deadline = time.time() + timeout
    while len(latencies) < events - failed and time.time() < deadline:
        message = consumer.poll(1.0)
        if message is None or message.error():
            continue
        import json

        body = json.loads(message.value() or b"{}")
        latencies.append((time.time() - float(body["data"]["sent_at"])) * 1000)
    consumer.close()
    latencies.sort()

    def pct(q: float) -> float | None:
        return (
            round(latencies[min(len(latencies) - 1, math.ceil(len(latencies) * q) - 1)], 1)
            if latencies
            else None
        )

    consume_seconds = max(time.time() - produce_started, 1e-6)
    return {
        "topic": topic,
        "produced": events - failed,
        "produceFailures": failed,
        "produceRatePerSecond": round((events - failed) / produce_seconds, 1),
        "consumed": len(latencies),
        "consumeThroughputPerSecond": round(len(latencies) / consume_seconds, 1),
        "latencyP50Ms": pct(0.50),
        "latencyP95Ms": pct(0.95),
        "latencyP99Ms": pct(0.99),
    }
