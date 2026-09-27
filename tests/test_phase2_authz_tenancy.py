from __future__ import annotations

import time
import uuid

import jwt as pyjwt
import pytest
from django.conf import settings
from django.urls import reverse

from apps.ai_gateway.models import Evaluation, Model, Prompt, Provider
from apps.conversations.models import ChatRequest, Conversation, Message
from apps.identity.authorization import tenant_scoped_queryset
from apps.identity.models import Organization, Role, UserRole
from apps.jobs.models import Job


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


@pytest.fixture
def api_model(db):
    provider = Provider.objects.create(name="Phase 2 Provider", slug="phase-2-provider", type="echo")
    return Model.objects.create(
        provider=provider,
        name="phase-2-model",
        display_name="Phase 2 Model",
        modality=Model.Modality.TEXT,
    )


@pytest.mark.django_db
def test_prompt_and_evaluation_endpoints_hide_other_tenant_resources(api_client, user, api_model):
    allowed = Organization.objects.create(name="Allowed tenant", slug="allowed-tenant", owner=user)
    user.organizations.add(allowed)
    other_user = type(user).objects.create_user(
        username="other-tenant-user", supabase_user_id="other-tenant-user", email="other@example.com"
    )
    forbidden = Organization.objects.create(
        name="Forbidden tenant", slug="forbidden-tenant", owner=other_user
    )
    other_user.organizations.add(forbidden)
    prompt = Prompt.objects.create(
        name="Forbidden prompt",
        slug="forbidden-prompt",
        category=Prompt.Category.TASK,
        content="secret",
        organization=forbidden,
        created_by=other_user,
    )
    evaluation = Evaluation.objects.create(
        name="Forbidden evaluation",
        slug="forbidden-evaluation",
        type=Evaluation.Type.ACCURACY,
        model=api_model,
        prompt=prompt,
        dataset_name="private",
        dataset_version="1",
        organization=forbidden,
        created_by=other_user,
    )
    api_client.force_authenticate(user)

    assert api_client.get(reverse("prompt-detail", kwargs={"slug": prompt.slug})).status_code == 404
    assert (
        api_client.patch(
            reverse("prompt-detail", kwargs={"slug": prompt.slug}), {"content": "changed"}
        ).status_code
        == 404
    )
    assert api_client.delete(reverse("prompt-detail", kwargs={"slug": prompt.slug})).status_code == 404
    assert api_client.get(reverse("evaluation-detail", kwargs={"slug": evaluation.slug})).status_code == 404
    assert (
        api_client.patch(
            reverse("evaluation-detail", kwargs={"slug": evaluation.slug}), {"dataset_name": "changed"}
        ).status_code
        == 404
    )
    assert (
        api_client.delete(reverse("evaluation-detail", kwargs={"slug": evaluation.slug})).status_code == 404
    )


@pytest.mark.django_db
def test_selected_organization_allows_nonprimary_prompt_creation(api_client, user):
    first = Organization.objects.create(name="First tenant", slug="first-tenant", owner=user)
    second = Organization.objects.create(name="Second tenant", slug="second-tenant", owner=user)
    user.organizations.add(first, second)
    api_client.force_authenticate(user)

    response = api_client.post(
        reverse("prompt-list"),
        {
            "name": "Second tenant prompt",
            "slug": "second-tenant-prompt",
            "category": Prompt.Category.TASK,
            "content": "hello",
        },
        HTTP_X_ORGANIZATION_ID=str(second.id),
    )

    assert response.status_code == 201, response.content
    assert Prompt.objects.get(slug="second-tenant-prompt").organization_id == second.id


@pytest.mark.django_db
def test_viewer_role_cannot_mutate_prompts(api_client, user):
    owner = type(user).objects.create_user(
        username="role-owner", supabase_user_id="role-owner", email="role-owner@example.com"
    )
    organization = Organization.objects.create(name="Role tenant", slug="role-tenant", owner=owner)
    user.organizations.add(organization)
    viewer = Role.objects.get(name=Role.RoleType.VIEWER)
    UserRole.objects.create(user=user, role=viewer, organization=organization)
    api_client.force_authenticate(user)

    response = api_client.post(
        reverse("prompt-list"),
        {"name": "Blocked", "slug": "viewer-blocked", "category": Prompt.Category.TASK, "content": "no"},
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_organization_member_can_list_team_conversations(api_client, user):
    owner = type(user).objects.create_user(
        username="conversation-owner", supabase_user_id="conversation-owner", email="owner@example.com"
    )
    organization = Organization.objects.create(name="Shared tenant", slug="shared-tenant", owner=owner)
    owner.organizations.add(organization)
    user.organizations.add(organization)
    conversation = Conversation.objects.create(
        owner=owner, organization=organization, title="Team conversation"
    )
    api_client.force_authenticate(user)

    response = api_client.get(reverse("conversation-list"))

    assert response.status_code == 200
    items = response.json().get("results", response.json())
    assert str(conversation.id) in {item["id"] for item in items}


@pytest.mark.django_db
def test_job_status_callback_reads_configured_secret(api_client, user, monkeypatch):
    organization = Organization.objects.create(name="Callback tenant", slug="callback-tenant", owner=user)
    user.organizations.add(organization)
    job = Job.objects.create(
        owner=user,
        organization=organization,
        task_type=Job.TaskType.GENERAL_QUESTION,
        input_payload={},
        trace_id="phase2-callback",
    )
    monkeypatch.setattr(settings, "N8N_WEBHOOK_SECRET", "phase2-webhook-secret")

    response = api_client.post(
        reverse("job-status-callback", kwargs={"job_id": job.id}),
        {"status": Job.Status.RUNNING},
        HTTP_X_JT_CODE_WEBHOOK_SECRET="phase2-webhook-secret",
    )

    assert response.status_code == 200, response.content
    job.refresh_from_db()
    assert job.status == Job.Status.RUNNING
