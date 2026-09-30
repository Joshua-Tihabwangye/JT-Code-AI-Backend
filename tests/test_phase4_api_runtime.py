from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.urls import reverse
from django.utils import timezone

from apps.ai_gateway.models import ModelRun
from apps.conversations.models import ChatRequest, Conversation, ConversationFeedback, Message
from apps.conversations.tasks import (
    dispatch_queued_chat_requests,
    process_chat_request,
    recover_stalled_chat_requests,
)
from apps.identity.models import Organization


@pytest.fixture
def conversation_organization(user):
    organization = Organization.objects.create(name="Conversation Org", slug="conversation-org", owner=user)
    user.organizations.add(organization)
    return organization


@pytest.fixture
def conversation(user, conversation_organization):
    return Conversation.objects.create(
        owner=user,
        organization=conversation_organization,
        title="Phase 4 conversation",
    )


@pytest.mark.django_db
def test_message_submission_replays_only_an_identical_request(
    authenticated_client, conversation, monkeypatch
):
    monkeypatch.setattr("apps.conversations.runtime_views.process_chat_request.apply_async", lambda **_: None)
    url = reverse("conversation-messages", kwargs={"pk": conversation.id})

    first = authenticated_client.post(
        url,
        {"content": "hello", "timezone": "UTC", "locale": "en"},
        HTTP_IDEMPOTENCY_KEY="phase4-message-key",
    )
    replay = authenticated_client.post(
        url,
        {"content": "hello", "timezone": "UTC", "locale": "en"},
        HTTP_IDEMPOTENCY_KEY="phase4-message-key",
    )
    conflict = authenticated_client.post(
        url,
        {"content": "different content"},
        HTTP_IDEMPOTENCY_KEY="phase4-message-key",
    )

    assert first.status_code == 202, first.content
    assert replay.status_code == 200, replay.content
    assert replay["Idempotency-Replayed"] == "true"
    assert replay.json()["id"] == first.json()["id"]
    assert conflict.status_code == 409, conflict.content
    assert conflict.json()["code"] == "idempotency_conflict"
    assert Message.objects.filter(conversation=conversation, role=Message.Role.USER).count() == 1


@pytest.mark.django_db
def test_conversation_message_and_feedback_contracts(authenticated_client, user, conversation):
    request = ChatRequest.objects.create(
        owner=user,
        organization=conversation.organization,
        conversation=conversation,
        idempotency_key="phase4-existing-request",
        request_fingerprint="a" * 64,
        input_text="input",
        trace_id="trace",
    )
    Message.objects.create(
        conversation=conversation,
        organization=conversation.organization,
        role=Message.Role.USER,
        content="input",
    )
    messages = authenticated_client.get(reverse("conversation-messages", kwargs={"pk": conversation.id}))
    created = authenticated_client.post(
        reverse("conversation-feedback", kwargs={"pk": conversation.id}),
        {"chatRequestId": str(request.id), "rating": 4, "comment": "Useful"},
    )
    updated = authenticated_client.post(
        reverse("conversation-feedback", kwargs={"pk": conversation.id}),
        {"chatRequestId": str(request.id), "rating": 5, "comment": "Very useful"},
    )

    assert messages.status_code == 200, messages.content
    assert messages.json()["results"][0]["content"] == "input"
    assert created.status_code == 201, created.content
    assert updated.status_code == 200, updated.content
    assert ConversationFeedback.objects.get(owner=user, chat_request=request).rating == 5


@pytest.mark.django_db
def test_conversation_filters_and_archiving(authenticated_client, conversation):
    listed = authenticated_client.get(reverse("conversation-list"), {"q": "Phase 4"})
    archived = authenticated_client.post(reverse("conversation-archive", kwargs={"pk": conversation.id}))
    hidden = authenticated_client.get(reverse("conversation-list"))
    included = authenticated_client.get(reverse("conversation-list"), {"includeArchived": "true"})

    assert listed.status_code == 200
    assert listed.json()["results"][0]["id"] == str(conversation.id)
    assert archived.status_code == 200
    assert hidden.json()["results"] == []
    assert included.json()["results"][0]["archivedAt"] is not None


@pytest.mark.django_db
def test_phase4_errors_use_the_public_envelope(authenticated_client, conversation):
    response = authenticated_client.post(
        reverse("conversation-messages", kwargs={"pk": conversation.id}),
        {"content": "hello"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid"
    assert response.json()["message"] == "Request validation failed."
    assert "idempotencyKey" in response.json()["details"]
    assert response.json()["requestId"]


@pytest.mark.django_db
def test_sse_is_authenticated_and_uses_terminal_event(authenticated_client, user, conversation):
    request = ChatRequest.objects.create(
        owner=user,
        organization=conversation.organization,
        conversation=conversation,
        idempotency_key="phase4-completed-request",
        request_fingerprint="b" * 64,
        input_text="input",
        output_text="output",
        status=ChatRequest.Status.COMPLETED,
        trace_id="trace",
    )

    response = authenticated_client.get(reverse("chat-request-stream", kwargs={"pk": request.id}))

    async def consume():
        return b"".join([chunk async for chunk in response.streaming_content])

    events = async_to_sync(consume)().decode()

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/event-stream")
    assert "event: completed" in events


@pytest.mark.django_db
def test_phase4_chat_list_accepts_tenant_scoped_status_filter(authenticated_client, user, conversation):
    ChatRequest.objects.create(
        owner=user,
        organization=conversation.organization,
        conversation=conversation,
        idempotency_key="phase4-list-request",
        request_fingerprint="c" * 64,
        input_text="input",
        status=ChatRequest.Status.FAILED,
        trace_id="trace",
    )

    response = authenticated_client.get(reverse("chat-request-list"), {"status": "failed"})

    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "failed"


def test_versioned_openapi_routes_are_public():
    assert reverse("schema-v1") == "/api/v1/schema/"
    assert reverse("swagger-ui-v1") == "/api/v1/docs/"


@pytest.mark.django_db
def test_chat_worker_uses_governed_gateway_and_records_auditable_result(user, conversation):
    request = ChatRequest.objects.create(
        owner=user,
        organization=conversation.organization,
        conversation=conversation,
        idempotency_key="phase4-worker-success",
        request_fingerprint="d" * 64,
        input_text="hello",
        trace_id="phase4-worker",
    )
    Message.objects.create(
        conversation=conversation,
        organization=conversation.organization,
        role=Message.Role.USER,
        content="hello",
    )

    result = process_chat_request.delay(str(request.id)).get()

    request.refresh_from_db()
    assert result["status"] == "completed"
    assert request.status == ChatRequest.Status.COMPLETED
    assert request.model_run_id is not None
    assert request.model_run.status == ModelRun.Status.COMPLETED
    assert request.provider_name
    assert request.model_name
    assert Message.objects.filter(conversation=conversation, role=Message.Role.ASSISTANT).exists()


@pytest.mark.django_db
def test_chat_worker_surfaces_permanent_gateway_failure_without_retry(user, conversation, settings):
    settings.AI_PROVIDER = "disabled"
    request = ChatRequest.objects.create(
        owner=user,
        organization=conversation.organization,
        conversation=conversation,
        idempotency_key="phase4-worker-failure",
        request_fingerprint="e" * 64,
        input_text="hello",
        trace_id="phase4-worker-failure",
    )
    Message.objects.create(
        conversation=conversation,
        organization=conversation.organization,
        role=Message.Role.USER,
        content="hello",
    )

    result = process_chat_request.delay(str(request.id)).get()

    request.refresh_from_db()
    assert result == {"status": "failed", "error_code": "AI_PROVIDER_NOT_CONFIGURED"}
    assert request.status == ChatRequest.Status.FAILED
    assert request.retry_count == 0
    assert request.model_run_id is not None
    assert request.model_run.status == ModelRun.Status.FAILED


@pytest.mark.django_db
def test_chat_cancellation_is_tenant_scoped_and_revokes_known_task(
    authenticated_client, user, conversation, monkeypatch, django_capture_on_commit_callbacks
):
    request = ChatRequest.objects.create(
        owner=user,
        organization=conversation.organization,
        conversation=conversation,
        idempotency_key="phase4-cancel",
        request_fingerprint="f" * 64,
        input_text="hello",
        trace_id="phase4-cancel",
        celery_task_id="broker-task-id",
    )
    revoked = []
    monkeypatch.setattr(
        "apps.conversations.runtime_views.current_app.control.revoke",
        lambda task_id: revoked.append(task_id),
    )

    with django_capture_on_commit_callbacks(execute=True):
        response = authenticated_client.post(reverse("chat-request-cancel", kwargs={"pk": request.id}))

    request.refresh_from_db()
    assert response.status_code == 200, response.content
    assert request.status == ChatRequest.Status.CANCELLED
    assert request.cancel_requested_at is not None
    assert revoked == ["broker-task-id"]


@pytest.mark.django_db
def test_stalled_chat_recovery_requeues_only_stale_non_cancelled_work(
    user, conversation, monkeypatch, settings
):
    settings.CHAT_REQUEST_STALLED_TIMEOUT_SECONDS = 30
    stale = ChatRequest.objects.create(
        owner=user,
        organization=conversation.organization,
        conversation=conversation,
        idempotency_key="phase4-recover-stale",
        request_fingerprint="g" * 64,
        input_text="hello",
        trace_id="phase4-recover",
        status=ChatRequest.Status.RUNNING,
        started_at=timezone.now() - timedelta(minutes=2),
    )
    cancelled = ChatRequest.objects.create(
        owner=user,
        organization=conversation.organization,
        conversation=conversation,
        idempotency_key="phase4-recover-cancelled",
        request_fingerprint="h" * 64,
        input_text="hello",
        trace_id="phase4-recover-cancelled",
        status=ChatRequest.Status.RUNNING,
        started_at=timezone.now() - timedelta(minutes=2),
        cancel_requested_at=timezone.now(),
    )
    dispatched = []

    def fake_apply_async(*, args, task_id):
        dispatched.append({"args": args, "task_id": task_id})
        return SimpleNamespace(id=task_id)

    monkeypatch.setattr("apps.conversations.tasks.process_chat_request.apply_async", fake_apply_async)

    assert recover_stalled_chat_requests() == 1
    stale.refresh_from_db()
    cancelled.refresh_from_db()
    assert stale.status == ChatRequest.Status.QUEUED
    assert stale.celery_task_id == dispatched[0]["task_id"]
    assert dispatched == [{"args": [str(stale.id)], "task_id": stale.celery_task_id}]
    assert stale.error_code == "WORKER_RECOVERY"
    assert cancelled.status == ChatRequest.Status.RUNNING


@pytest.mark.django_db
def test_broker_outage_keeps_chat_request_durable_until_periodic_dispatch(
    authenticated_client, conversation, monkeypatch, django_capture_on_commit_callbacks
):
    monkeypatch.setattr(
        "apps.conversations.runtime_views.process_chat_request.apply_async",
        lambda **_: (_ for _ in ()).throw(ConnectionError("broker unavailable")),
    )
    with django_capture_on_commit_callbacks(execute=True):
        response = authenticated_client.post(
            reverse("conversation-messages", kwargs={"pk": conversation.id}),
            {"content": "recover after broker outage"},
            HTTP_IDEMPOTENCY_KEY="phase4-broker-outage",
        )

    request = ChatRequest.objects.get(id=response.json()["id"])
    assert response.status_code == 202, response.content
    assert request.status == ChatRequest.Status.QUEUED
    durable_task_id = request.celery_task_id
    assert durable_task_id

    dispatches = []

    def fake_apply_async(*, args, task_id):
        dispatches.append({"args": args, "task_id": task_id})
        return SimpleNamespace(id=task_id)

    monkeypatch.setattr(
        "apps.conversations.tasks.process_chat_request.apply_async",
        fake_apply_async,
    )
    assert dispatch_queued_chat_requests() == 1

    request.refresh_from_db()
    assert request.celery_task_id == durable_task_id
    assert dispatches == [{"args": [str(request.id)], "task_id": durable_task_id}]
