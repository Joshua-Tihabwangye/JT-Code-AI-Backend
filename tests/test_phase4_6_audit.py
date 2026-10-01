"""Regression guards for the Phase 4-6 production-readiness audit (2026-09-30).

Each test is named after the defect it proves fixed; the audit finding ID is in
its docstring. A new open defect can be recorded with ``audit_xfail`` so the
suite stays green until the fix lands (strict mode then forces its removal).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.db import OperationalError
from django.urls import reverse
from django.utils import timezone

from apps.conversations.models import ChatRequest, Conversation
from apps.events.consumers import register_handler
from apps.events.contracts import build_envelope, transport_headers
from apps.events.models import DeadLetterEvent, OutboxEvent
from apps.events.tasks import publish_outbox_batch
from apps.identity.models import Organization, Role, UserRole
from apps.jobs.executor import HANDLERS, execute_job
from apps.jobs.models import Job
from apps.jobs.transitions import apply_status_update


def audit_xfail(finding: str, reason: str):
    return pytest.mark.xfail(strict=True, reason=f"[{finding}] {reason}")


@pytest.fixture
def org(user):
    organization = Organization.objects.create(name="Audit Org", owner=user)
    user.organizations.add(organization)
    return organization


@pytest.fixture
def other_user(django_user_model):
    return django_user_model.objects.create_user(
        username="audit-other", supabase_user_id="audit-other-sub", email="other@example.com"
    )


@pytest.fixture
def viewer_org(user, other_user, org):
    """A second tenant where ``user`` is only a viewer (joined after ``org``)."""
    organization = Organization.objects.create(name="Viewer Org", owner=other_user)
    other_user.organizations.add(organization)
    user.organizations.add(organization)  # identity.signals assigns the baseline viewer role
    assert UserRole.objects.filter(
        user=user, organization=organization, role__name=Role.RoleType.VIEWER
    ).exists()
    return organization


def make_job(user, org, **kwargs):
    return Job.objects.create(
        owner=user,
        organization=org,
        task_type=kwargs.pop("task_type", Job.TaskType.GENERAL_QUESTION),
        trace_id="audit",
        input_payload=kwargs.pop("input_payload", {"messages": [{"role": "user", "content": "hi"}]}),
        **kwargs,
    )


# --------------------------------------------------------------------------- migrations


def test_pgvector_migration_creates_extension_and_column_before_index():
    """MIG-1: extension -> column -> index ordering on a fresh database."""
    from importlib import import_module

    from django.db import migrations

    module = import_module("apps.knowledge.migrations.0003_add_pgvector_embeddings")
    operations = module.Migration.operations
    add_embedding = next(
        index
        for index, op in enumerate(operations)
        if isinstance(op, migrations.AddField) and op.name == "embedding"
    )
    run_python = {
        op.code: index for index, op in enumerate(operations) if isinstance(op, migrations.RunPython)
    }
    assert run_python[module.enable_pgvector] < add_embedding < run_python[module.create_embedding_index]


# --------------------------------------------------------------------------- Phase 4


@pytest.mark.django_db
def test_chat_request_create_checks_write_access_on_the_conversations_org(
    authenticated_client, other_user, viewer_org, monkeypatch
):
    """P4-1: writes are authorized against the conversation's own tenant."""
    monkeypatch.setattr("apps.conversations.runtime_views.process_chat_request.apply_async", lambda **_: None)
    conversation = Conversation.objects.create(owner=other_user, organization=viewer_org, title="Theirs")

    response = authenticated_client.post(
        "/api/v1/chat/requests/",
        {"conversationId": str(conversation.id), "chatInput": "viewer write"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="audit-p4-1",
    )

    assert response.status_code in {403, 404}, response.content
    assert not ChatRequest.objects.filter(conversation=conversation).exists()


@pytest.mark.django_db
def test_control_viewer_cannot_post_messages_through_the_conversation_route(
    authenticated_client, other_user, viewer_org
):
    """Control for P4-1: the equivalent nested route is already correctly denied."""
    conversation = Conversation.objects.create(owner=other_user, organization=viewer_org, title="Theirs")

    response = authenticated_client.post(
        reverse("conversation-messages", kwargs={"pk": conversation.id}),
        {"content": "viewer write"},
        HTTP_IDEMPOTENCY_KEY="audit-p4-1-control",
    )

    assert response.status_code == 403, response.content


@pytest.mark.django_db
def test_chat_dispatcher_does_not_republish_requests_waiting_for_retry_backoff(user, org, monkeypatch):
    """P4-2: the safety-net dispatcher honours dispatch_after (retry backoff)."""
    from apps.conversations.tasks import dispatch_queued_chat_requests

    conversation = Conversation.objects.create(owner=user, organization=org)
    ChatRequest.objects.create(
        owner=user,
        organization=org,
        conversation=conversation,
        idempotency_key="audit-p4-2",
        request_fingerprint="x",
        input_text="hello",
        trace_id="audit",
        celery_task_id="task-already-scheduled-for-retry",
        retry_count=1,
        last_retry_at=timezone.now(),
        dispatch_after=timezone.now() + timedelta(seconds=120),
    )
    published = []
    monkeypatch.setattr(
        "apps.conversations.tasks.process_chat_request.apply_async",
        lambda **kwargs: published.append(kwargs),
    )

    dispatch_queued_chat_requests.run()

    assert published == []


@pytest.mark.django_db
def test_messages_cannot_be_posted_to_an_archived_conversation(authenticated_client, user, org, monkeypatch):
    """P4-3: archived conversations reject new messages."""
    monkeypatch.setattr("apps.conversations.runtime_views.process_chat_request.apply_async", lambda **_: None)
    conversation = Conversation.objects.create(
        owner=user, organization=org, title="Archived", archived_at=timezone.now()
    )

    response = authenticated_client.post(
        reverse("conversation-messages", kwargs={"pk": conversation.id}) + "?includeArchived=true",
        {"content": "into the archive"},
        HTTP_IDEMPOTENCY_KEY="audit-p4-3",
    )

    assert response.status_code in {400, 404, 409}, response.content


@pytest.mark.django_db
def test_readiness_kafka_probe_uses_configured_security(api_client, settings, monkeypatch):
    """P4-4: the readiness probe authenticates like every other Kafka client."""
    settings.HEALTHCHECK_EXTERNAL_DEPENDENCIES = True
    settings.KAFKA_BOOTSTRAP_SERVERS = "kafka.example.test:9093"
    settings.KAFKA_SECURITY_PROTOCOL = "SASL_SSL"
    settings.KAFKA_SASL_MECHANISM = "SCRAM-SHA-512"
    settings.KAFKA_SASL_USERNAME = "user"
    settings.KAFKA_SASL_PASSWORD = "password"
    configs = []

    class FakeAdminClient:
        def __init__(self, config):
            configs.append(config)

        def list_topics(self, timeout):
            return {}

    monkeypatch.setattr("confluent_kafka.admin.AdminClient", FakeAdminClient)
    fake_connection = SimpleNamespace(ensure_connection=lambda **_: None)
    monkeypatch.setattr(
        "apps.core.views.current_app", SimpleNamespace(connection_for_read=lambda: fake_connection)
    )

    api_client.get("/api/v1/health/ready/")

    assert configs and configs[0].get("security.protocol") == "SASL_SSL"
    assert configs[0].get("sasl.mechanism") == "SCRAM-SHA-512"


# --------------------------------------------------------------------------- Phase 5


@pytest.mark.django_db
def test_job_status_is_not_client_writable(authenticated_client, user, org):
    """P5-1: job state is owned by the state machine, never client PATCH."""
    job = make_job(user, org)

    authenticated_client.patch(f"/api/v1/jobs/{job.id}/", {"status": "completed"}, format="json")

    job.refresh_from_db()
    assert job.status == Job.Status.QUEUED


@pytest.mark.django_db
def test_jobs_cannot_be_deleted_through_the_api(authenticated_client, user, org):
    """P5-2: durable jobs cannot be deleted through the API."""
    job = make_job(user, org)

    authenticated_client.delete(f"/api/v1/jobs/{job.id}/")

    assert Job.objects.filter(id=job.id).exists()


@pytest.mark.django_db
def test_job_cannot_reference_another_tenants_conversation(authenticated_client, user, org, other_user):
    """P5-3: no cross-tenant conversation linking or title disclosure."""
    foreign_org = Organization.objects.create(name="Foreign", owner=other_user)
    other_user.organizations.add(foreign_org)
    foreign = Conversation.objects.create(owner=other_user, organization=foreign_org, title="Secret title")
    job = make_job(user, org)

    response = authenticated_client.patch(
        f"/api/v1/jobs/{job.id}/", {"conversation": str(foreign.id)}, format="json"
    )

    job.refresh_from_db()
    assert job.conversation_id != foreign.id
    assert "Secret title" not in response.content.decode()


@pytest.mark.django_db
def test_expired_job_is_not_overwritten_by_a_late_handler_completion(user, org, monkeypatch):
    """P5-4: a late handler result cannot override an expired job."""
    job = make_job(user, org)

    def slow_handler(running_job):
        apply_status_update(running_job.id, {"status": Job.Status.EXPIRED, "error_message": "deadline"})
        return {"answer": "too late", "usage": {}}

    monkeypatch.setitem(HANDLERS, Job.TaskType.GENERAL_QUESTION, slow_handler)

    execute_job(job)

    job.refresh_from_db()
    assert job.status == Job.Status.EXPIRED
    terminal_types = [
        topic.rsplit(".", 1)[-1]
        for topic in OutboxEvent.objects.filter(topic__contains="jobs.job.").values_list("topic", flat=True)
    ]
    assert terminal_types.count("expired") == 1
    assert "completed" not in terminal_types


@pytest.mark.django_db
def test_job_creation_without_credits_is_a_client_error(authenticated_client, org):
    """P5-5: an unfunded wallet yields 402, not 500."""
    authenticated_client.raise_request_exception = False

    response = authenticated_client.post(
        "/api/v1/jobs/",
        {
            "idempotency_key": "audit-p5-5",
            "task_type": Job.TaskType.GENERAL_QUESTION,
            "input_payload": {"messages": [{"role": "user", "content": "hi"}]},
        },
        format="json",
    )

    assert response.status_code == 402, response.content


# --------------------------------------------------------------------------- Phase 6


class FakeKafkaMessage:
    def __init__(self, envelope, *, topic, offset=7):
        self._value = json.dumps(envelope.as_dict()).encode()
        self._headers = [(k, v.encode()) for k, v in transport_headers(envelope).items()]
        self._topic = topic
        self._offset = offset

    def error(self):
        return None

    def value(self):
        return self._value

    def headers(self):
        return self._headers

    def topic(self):
        return self._topic

    def partition(self):
        return 0

    def offset(self):
        return self._offset


def fake_consumer_factory(*, messages=(), commit_failures=0, captured=None):
    """Return a Consumer stand-in that yields ``messages`` then stops the loop."""
    captured = captured if captured is not None else {}

    class FakeConsumer:
        def __init__(self, config):
            captured["config"] = config
            self._messages = list(messages)
            self._commit_failures = commit_failures
            captured["commits"] = 0

        def subscribe(self, topics):
            captured["topics"] = topics

        def poll(self, _timeout):
            if self._messages:
                return self._messages.pop(0)
            raise KeyboardInterrupt

        def commit(self, message, asynchronous):
            if self._commit_failures:
                self._commit_failures -= 1
                raise RuntimeError("KafkaException: commit failed during rebalance")
            captured["commits"] += 1

        def close(self):
            captured["closed"] = True

    return FakeConsumer


def run_consumer(topic: str, consumer_name: str = "audit"):
    from django.core.management import call_command

    # The fake consumer raises KeyboardInterrupt to stop the command's infinite poll loop.
    with contextlib.suppress(KeyboardInterrupt, Exception):
        call_command("run_kafka_consumer", topic, consumer_name=consumer_name)


@pytest.fixture
def inbound_webhook(user, org):
    from apps.integrations.models import Webhook

    return Webhook.objects.create(
        organization=org,
        name="Inbound",
        url="https://callbacks.example.test/in",
        secret="s3cret",
        created_by=user,
    )


def signed_webhook_request(webhook, *, body=None, timestamp=None, secret=None):
    from rest_framework.test import APIRequestFactory

    from apps.integrations.views import inbound_webhook_signature

    body = body if body is not None else json.dumps({"hello": "world", "nonce": uuid.uuid4().hex})
    timestamp = str(timestamp if timestamp is not None else int(time.time()))
    signature = inbound_webhook_signature(secret or webhook.secret, timestamp, body.encode())
    return APIRequestFactory().post(
        f"/api/v1/inbound-webhooks/{webhook.id}/",
        data=body,
        content_type="application/json",
        HTTP_X_WEBHOOK_SIGNATURE=signature,
        HTTP_X_WEBHOOK_TIMESTAMP=timestamp,
    )


@pytest.mark.django_db
def test_incoming_webhook_url_reaches_the_public_receiver(api_client, inbound_webhook):
    """P6-6: the public receiver is not shadowed by the router."""
    from django.urls import resolve

    from apps.integrations.views import IncomingWebhookView

    request = signed_webhook_request(inbound_webhook)
    view = resolve(request.path).func
    assert getattr(view, "view_class", getattr(view, "cls", None)) is IncomingWebhookView
    response = api_client.post(
        request.path,
        data=request.body,
        content_type="application/json",
        HTTP_X_WEBHOOK_SIGNATURE=request.headers["X-Webhook-Signature"],
        HTTP_X_WEBHOOK_TIMESTAMP=request.headers["X-Webhook-Timestamp"],
    )
    assert response.status_code == 200, response.content
    replay = api_client.post(
        request.path,
        data=request.body,
        content_type="application/json",
        HTTP_X_WEBHOOK_SIGNATURE=request.headers["X-Webhook-Signature"],
        HTTP_X_WEBHOOK_TIMESTAMP=request.headers["X-Webhook-Timestamp"],
    )
    assert replay.status_code == 409, replay.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    "tamper",
    ["wrong_secret", "stale_timestamp", "legacy_unkeyed_sha256"],
)
def test_incoming_webhook_rejects_forged_or_stale_signatures(inbound_webhook, tamper):
    """P6-6 hardening: timestamped HMAC-SHA256 replaces the forgeable sha256(secret + body)."""
    from apps.integrations.views import IncomingWebhookView

    if tamper == "wrong_secret":
        request = signed_webhook_request(inbound_webhook, secret="not-the-secret")
    elif tamper == "stale_timestamp":
        request = signed_webhook_request(inbound_webhook, timestamp=int(time.time()) - 3600)
    else:
        body = json.dumps({"hello": "world"})
        request = signed_webhook_request(inbound_webhook, body=body)
        request.META["HTTP_X_WEBHOOK_SIGNATURE"] = hashlib.sha256(
            (inbound_webhook.secret + body).encode()
        ).hexdigest()

    response = IncomingWebhookView.as_view()(request, webhook_id=inbound_webhook.id)

    assert response.status_code == 401
    assert not OutboxEvent.objects.filter(topic__endswith="integrations.webhook.received").exists()


@pytest.mark.django_db
def test_webhook_event_topic_matches_the_documented_consumer_subscription(inbound_webhook, monkeypatch):
    """P6-1: producers and consumers agree on prefixed topics."""
    from apps.integrations.views import IncomingWebhookView

    # Call the view directly so this proof is independent of the P6-6 routing defect.
    response = IncomingWebhookView.as_view()(
        signed_webhook_request(inbound_webhook), webhook_id=inbound_webhook.id
    )
    assert response.status_code == 200, response.data
    produced_topic = OutboxEvent.objects.get(event_key=response.data["delivery_id"]).topic

    captured: dict = {}
    monkeypatch.setattr(
        "apps.events.management.commands.run_kafka_consumer.Consumer",
        fake_consumer_factory(captured=captured),
    )
    run_consumer("integrations.webhook.received", consumer_name="integration-webhooks")

    assert produced_topic in captured["topics"]


@pytest.mark.django_db
def test_outbox_envelope_preserves_the_originating_request_trace_id(
    authenticated_client, user, org, monkeypatch
):
    """P6-2: correlation IDs are captured when the outbox row is written."""
    monkeypatch.setattr("apps.conversations.runtime_views.process_chat_request.apply_async", lambda **_: None)
    conversation = Conversation.objects.create(owner=user, organization=org)
    authenticated_client.post(
        reverse("conversation-messages", kwargs={"pk": conversation.id}),
        {"content": "trace me"},
        HTTP_IDEMPOTENCY_KEY="audit-p6-2",
        HTTP_X_REQUEST_ID="req-audit-p6-2",
        HTTP_X_TRACE_ID="trace-audit-p6-2",
    )
    envelopes = []
    monkeypatch.setattr(
        "apps.events.tasks.publish_many",
        lambda records: {envelopes.append(r[2]) or r[2].event_id: None for r in records},
    )

    publish_outbox_batch.run()

    accepted = next(e for e in envelopes if e.event_type == "chat.request.accepted")
    assert accepted.request_id == "req-audit-p6-2"
    assert accepted.trace_id == "trace-audit-p6-2"


@pytest.mark.django_db
def test_consumer_starts_against_a_plaintext_broker_without_sasl(settings, monkeypatch):
    """P6-3: the consumer starts without SASL on PLAINTEXT brokers."""
    from confluent_kafka import Consumer as RealConsumer

    settings.KAFKA_BOOTSTRAP_SERVERS = "localhost:9092"
    settings.KAFKA_SECURITY_PROTOCOL = "PLAINTEXT"
    settings.KAFKA_SASL_MECHANISM = ""
    settings.KAFKA_SASL_USERNAME = ""
    settings.KAFKA_SASL_PASSWORD = ""
    started = []

    def validating_consumer(config):
        RealConsumer(config).close()  # librdkafka validates config at construction; no broker needed
        started.append(True)
        return fake_consumer_factory()(config)

    monkeypatch.setattr("apps.events.management.commands.run_kafka_consumer.Consumer", validating_consumer)
    run_consumer("integrations.webhook.received")

    assert started == [True]


@pytest.mark.django_db
def test_offset_commit_failure_does_not_dead_letter_a_processed_event(settings, monkeypatch):
    """P6-4: an offset-commit failure never creates a false dead letter."""
    event_type = f"audit.commit.{uuid.uuid4().hex}"
    handled = []
    register_handler(event_type)(lambda envelope: handled.append(envelope.event_id))
    envelope = build_envelope(event_id=str(uuid.uuid4()), event_type=event_type, payload={})
    topic = f"{settings.KAFKA_TOPIC_PREFIX}.{event_type}"
    monkeypatch.setattr(
        "apps.events.management.commands.run_kafka_consumer.Consumer",
        fake_consumer_factory(messages=[FakeKafkaMessage(envelope, topic=topic)], commit_failures=1),
    )

    run_consumer(event_type)

    assert handled == [envelope.event_id]
    assert not DeadLetterEvent.objects.exists()


@pytest.mark.django_db
def test_transient_handler_error_is_retried_not_dead_lettered(settings, monkeypatch):
    """P6-5: a transient handler failure is retried in place instead of dead-lettered."""
    event_type = f"audit.transient.{uuid.uuid4().hex}"
    attempts = []

    def flaky_handler(envelope):
        attempts.append(envelope.event_id)
        if len(attempts) == 1:
            raise OperationalError("could not serialize access due to concurrent update")

    register_handler(event_type)(flaky_handler)
    envelope = build_envelope(event_id=str(uuid.uuid4()), event_type=event_type, payload={})
    topic = f"{settings.KAFKA_TOPIC_PREFIX}.{event_type}"
    captured: dict = {}
    monkeypatch.setattr(
        "apps.events.management.commands.run_kafka_consumer.Consumer",
        fake_consumer_factory(messages=[FakeKafkaMessage(envelope, topic=topic)], captured=captured),
    )

    run_consumer(event_type)

    assert len(attempts) == 2
    assert not DeadLetterEvent.objects.exists()
    assert captured["commits"] == 1


@pytest.mark.django_db
def test_control_malformed_event_is_dead_lettered_and_committed(settings, monkeypatch):
    """Control for P6-4/P6-5: genuine poison messages must still be dead-lettered."""
    envelope = build_envelope(
        event_id=str(uuid.uuid4()), event_type="audit.unregistered.event", payload={"x": 1}
    )
    topic = f"{settings.KAFKA_TOPIC_PREFIX}.audit.unregistered.event"
    captured: dict = {}
    monkeypatch.setattr(
        "apps.events.management.commands.run_kafka_consumer.Consumer",
        fake_consumer_factory(messages=[FakeKafkaMessage(envelope, topic=topic)], captured=captured),
    )

    run_consumer("audit.unregistered.event")

    assert DeadLetterEvent.objects.filter(topic=topic).count() == 1
    assert captured["commits"] == 1


def test_migrations_referencing_organization_depend_on_its_creation():
    """MIG-2: every migration touching identity.Organization must (transitively) follow identity.0003.

    A missing dependency only surfaces on a fresh database, when the planner happens
    to order the referencing app before the model exists.
    """
    from django.db.migrations.loader import MigrationLoader

    creator = ("identity", "0003_alter_user_options_alter_user_groups_and_more")
    loader = MigrationLoader(None, ignore_no_migrations=True)  # graph only, no database

    def references_organization(operation) -> bool:
        fields = [field for _name, field in getattr(operation, "fields", [])]
        if getattr(operation, "field", None) is not None:
            fields.append(operation.field)
        return any(
            str(getattr(field.remote_field, "model", "")).lower() == "identity.organization"
            for field in fields
            if getattr(field, "remote_field", None) is not None
        )

    offenders = [
        f"{key[0]}.{key[1]}"
        for key, migration in loader.disk_migrations.items()
        if key[0] != "identity"
        and any(references_organization(operation) for operation in migration.operations)
        and creator not in loader.graph.forwards_plan(key)
    ]
    assert offenders == []
