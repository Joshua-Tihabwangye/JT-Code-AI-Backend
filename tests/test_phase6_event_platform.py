"""Phase 6 proofs for versioned Kafka events and durable consumption."""

import uuid
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.utils import timezone

from apps.core.context import request_id_var, trace_id_var
from apps.events.consumers import dead_letter_event, process_event, register_handler
from apps.events.contracts import EventContractError, build_envelope, parse_envelope
from apps.events.models import ConsumedEvent, DeadLetterEvent, OutboxEvent
from apps.events.outbox import add_outbox_event, enqueue_outbox_event
from apps.events.tasks import publish_outbox_batch


def test_envelope_is_versioned_and_carries_correlation_context():
    request_token = request_id_var.set("request-6")
    trace_token = trace_id_var.set("trace-6")
    event_id = str(uuid.uuid4())
    try:
        envelope = build_envelope(
            event_id=event_id,
            event_type="jobs.job.created",
            payload={"job_id": "job-6"},
        )
    finally:
        request_id_var.reset(request_token)
        trace_id_var.reset(trace_token)

    assert envelope.schema_version == 1
    assert envelope.request_id == "request-6"
    assert envelope.trace_id == "trace-6"
    assert parse_envelope(envelope.as_dict()) == envelope


def test_invalid_or_unknown_event_contract_is_rejected():
    with pytest.raises(EventContractError):
        parse_envelope({"event_id": str(uuid.uuid4())})
    with pytest.raises(EventContractError):
        parse_envelope(
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "jobs.job.created",
                "schema_version": 999,
                "occurred_at": timezone.now().isoformat(),
                "data": {},
                "request_id": "",
                "trace_id": "",
            }
        )


@pytest.mark.django_db
def test_outbox_publisher_emits_envelope_and_transport_headers(monkeypatch):
    event = enqueue_outbox_event(
        topic="jobs.job.created",
        event_key="job-6",
        payload={"job_id": "job-6"},
        headers={"request_id": "request-6", "trace_id": "trace-6"},
    )
    sent = []

    def fake_publish(topic, key, envelope, headers):
        sent.append((topic, key, envelope, headers))

    monkeypatch.setattr("apps.events.tasks.publish", fake_publish)

    assert publish_outbox_batch.run() == 1

    event.refresh_from_db()
    assert event.status == OutboxEvent.Status.PUBLISHED
    assert sent[0][2].event_id == str(event.id)
    assert sent[0][2].event_type == "jobs.job.created"
    assert sent[0][2].request_id == "request-6"


@pytest.mark.django_db(transaction=True)
def test_outbox_network_publish_runs_outside_database_transaction(monkeypatch):
    enqueue_outbox_event(topic="jobs.job.created", event_key="job-6", payload={})
    transaction_states = []

    def fake_publish(*_args):
        transaction_states.append(connection.in_atomic_block)

    monkeypatch.setattr("apps.events.tasks.publish", fake_publish)
    assert publish_outbox_batch.run() == 1
    assert transaction_states == [False]


@pytest.mark.django_db
def test_outbox_failure_uses_bounded_backoff(monkeypatch, settings):
    settings.EVENT_OUTBOX_MAX_ATTEMPTS = 2
    event = enqueue_outbox_event(topic="jobs.job.created", event_key="job-6", payload={})
    monkeypatch.setattr(
        "apps.events.tasks.publish", lambda *_args: (_ for _ in ()).throw(TimeoutError("down"))
    )

    assert publish_outbox_batch.run() == 0

    event.refresh_from_db()
    assert event.status == OutboxEvent.Status.PENDING
    assert event.attempts == 1
    assert event.available_at > event.created_at


@pytest.mark.django_db
def test_expired_publisher_lease_is_reclaimed(monkeypatch, settings):
    settings.EVENT_OUTBOX_LEASE_SECONDS = 1
    event = enqueue_outbox_event(topic="jobs.job.created", event_key="job-6", payload={})
    event.status = OutboxEvent.Status.PUBLISHING
    event.attempts = 1
    event.publishing_started_at = timezone.now() - timedelta(seconds=2)
    event.publishing_token = uuid.uuid4()
    event.save()
    monkeypatch.setattr("apps.events.tasks.publish", lambda *_args: None)

    assert publish_outbox_batch.run() == 1
    event.refresh_from_db()
    assert event.status == OutboxEvent.Status.PUBLISHED
    assert event.attempts == 2


@pytest.mark.django_db
def test_consumer_handler_is_idempotent_per_group():
    event_type = f"tests.phase6.{uuid.uuid4()}"
    delivered = []

    @register_handler(event_type)
    def handle(envelope):
        delivered.append(envelope.data["value"])

    envelope = build_envelope(event_id=str(uuid.uuid4()), event_type=event_type, payload={"value": "once"})

    assert process_event(
        consumer_group="jt-code.phase6", envelope=envelope, topic="jt-code.tests", partition=0, offset=10
    )
    assert not process_event(
        consumer_group="jt-code.phase6", envelope=envelope, topic="jt-code.tests", partition=0, offset=11
    )
    assert delivered == ["once"]
    assert (
        ConsumedEvent.objects.filter(consumer_group="jt-code.phase6", event_id=envelope.event_id).count() == 1
    )


@pytest.mark.django_db
def test_rejected_consumer_event_is_durable_and_notified_through_outbox():
    dead_letter = dead_letter_event(
        consumer_group="jt-code.phase6",
        topic="jt-code.jobs.job.created",
        payload={"event_id": str(uuid.uuid4()), "event_type": "jobs.job.created"},
        headers={"request_id": "request-6", "trace_id": "trace-6"},
        partition=1,
        offset=99,
        error="unsupported event",
    )

    assert DeadLetterEvent.objects.filter(id=dead_letter.id).exists()
    notification = OutboxEvent.objects.get(event_key=str(dead_letter.id))
    assert notification.topic.endswith("events.dead_lettered")
    assert notification.payload["source_topic"] == "jt-code.jobs.job.created"


@pytest.mark.django_db
def test_duplicate_dead_letter_source_does_not_duplicate_notifications():
    payload = {"event_id": str(uuid.uuid4()), "event_type": "jobs.job.created"}
    first = dead_letter_event(
        consumer_group="jt-code.phase6",
        topic="jt-code.jobs.job.created",
        payload=payload,
        headers={},
        partition=1,
        offset=99,
        error="unsupported event",
    )
    second = dead_letter_event(
        consumer_group="jt-code.phase6",
        topic="jt-code.jobs.job.created",
        payload=payload,
        headers={},
        partition=1,
        offset=99,
        error="unsupported event",
    )

    assert second.id == first.id
    assert DeadLetterEvent.objects.count() == 1
    assert OutboxEvent.objects.filter(event_key=str(first.id)).count() == 1


@pytest.mark.django_db
def test_operator_replay_creates_a_new_event_and_can_only_run_once():
    envelope = build_envelope(
        event_id=str(uuid.uuid4()),
        event_type="jobs.job.created",
        payload={"job_id": "job-6"},
    )
    dead_letter = dead_letter_event(
        consumer_group="jt-code.phase6",
        topic="jt-code.jobs.job.created",
        payload=envelope.as_dict(),
        headers={},
        error="fixed now",
    )

    call_command("replay_dead_letter", str(dead_letter.id), "--confirm", "--actor", "operator-6")
    replay = OutboxEvent.objects.exclude(event_key=str(dead_letter.id)).get()
    assert replay.topic.endswith("jobs.job.created")
    assert replay.payload == {"job_id": "job-6"}
    assert replay.headers["causation_id"] == envelope.event_id
    dead_letter.refresh_from_db()
    assert dead_letter.replayed_at is not None
    with pytest.raises(CommandError):
        call_command("replay_dead_letter", str(dead_letter.id), "--confirm")


@pytest.mark.django_db
def test_prefixed_outbox_helper_keeps_event_contract_name():
    event = add_outbox_event("assets.asset.created", "asset-6", {"asset_id": "asset-6"})

    assert event.topic.endswith("assets.asset.created")
