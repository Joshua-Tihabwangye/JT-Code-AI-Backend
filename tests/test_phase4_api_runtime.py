from __future__ import annotations

import pytest
from django.urls import reverse

from apps.conversations.models import ChatRequest, Conversation, ConversationFeedback, Message
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
    monkeypatch.setattr("apps.conversations.runtime_views.process_chat_request.delay", lambda *_: None)
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
    events = b"".join(response.streaming_content).decode()

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
