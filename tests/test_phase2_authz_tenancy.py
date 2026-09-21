from __future__ import annotations

import time
import uuid

import jwt as pyjwt
import pytest
from django.conf import settings
from django.urls import reverse

from apps.conversations.models import ChatRequest, Conversation, Message
from apps.identity.authorization import tenant_scoped_queryset
from apps.identity.models import Organization


def _token(*, sub: str, email: str, secret: str | None = None, issuer: str | None = None) -> str:
    payload = {
        "sub": sub,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "jti": str(uuid.uuid4()),
    }
    if issuer is not None:
        payload["iss"] = issuer
    return pyjwt.encode(payload, secret or settings.SUPABASE_JWT_SECRET, algorithm="HS256")


@pytest.fixture
def organization(db, user):
    org = Organization.objects.create(name="Phase 2 Org", slug="phase-2-org", owner=user)
    user.organizations.add(org)
    return org


@pytest.mark.django_db
def test_hmac_supabase_token_rejects_invalid_issuer(api_client, user, monkeypatch):
    monkeypatch.setattr("apps.identity.authentication.settings.SUPABASE_URL", "")
    monkeypatch.setattr(
        "apps.identity.authentication.settings.SUPABASE_JWT_ISSUER",
        "https://project.supabase.co/auth/v1",
    )
    api_client.credentials(
        HTTP_AUTHORIZATION=(
            "Bearer "
            + _token(
                sub=user.supabase_user_id,
                email=user.email,
                issuer="https://attacker.supabase.co/auth/v1",
            )
        )
    )

    response = api_client.get(reverse("auth-ping"))

    assert response.status_code == 401


@pytest.mark.django_db
def test_forged_hmac_supabase_token_is_rejected(api_client, user):
    api_client.credentials(
        HTTP_AUTHORIZATION="Bearer "
        + _token(sub=user.supabase_user_id, email=user.email, secret="wrong-secret")
    )

    response = api_client.get(reverse("auth-ping"))

    assert response.status_code == 401


@pytest.mark.django_db
def test_tenant_scoped_queryset_excludes_unjoined_organization_rows(user):
    allowed = Organization.objects.create(name="Allowed", slug="allowed", owner=user)
    forbidden = Organization.objects.create(name="Forbidden", slug="forbidden")
    user.organizations.add(allowed)
    allowed_conversation = Conversation.objects.create(owner=user, organization=allowed, title="Allowed")
    Conversation.objects.create(owner=user, organization=forbidden, title="Forbidden")

    visible = tenant_scoped_queryset(Conversation.objects.filter(owner=user), user)

    assert list(visible) == [allowed_conversation]


@pytest.mark.django_db
def test_chat_creation_propagates_organization(authenticated_client, user, organization):
    conversation_response = authenticated_client.post(
        reverse("conversation-list"),
        {"title": "Tenant chat"},
    )
    assert conversation_response.status_code == 201, conversation_response.content
    conversation_id = conversation_response.json()["id"]
    conversation = Conversation.objects.get(id=conversation_id)
    assert conversation.organization_id == organization.id

    chat_response = authenticated_client.post(
        reverse("chat-request-list"),
        {
            "conversationId": conversation_id,
            "chatInput": "hello",
        },
        HTTP_IDEMPOTENCY_KEY="phase2-chat-key",
    )

    assert chat_response.status_code == 202, chat_response.content
    chat_request = ChatRequest.objects.get(id=chat_response.json()["id"])
    assert chat_request.organization_id == organization.id
    assert Message.objects.filter(
        conversation=conversation,
        role=Message.Role.USER,
        organization=organization,
    ).exists()
