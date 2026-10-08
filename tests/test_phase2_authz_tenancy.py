from __future__ import annotations

import json
import time
import uuid

import jwt as pyjwt
import pytest
from django.conf import settings
from django.urls import reverse

from apps.ai_gateway.models import Evaluation, Model, Prompt, Provider
from apps.conversations.models import ChatRequest, Conversation, Message
from apps.identity.authorization import tenant_scoped_queryset
from apps.identity.models import Organization, Role, User, UserOrganization, UserRole
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
    # Membership lifecycle assigns a viewer role to non-owner members.
    assert UserRole.objects.filter(
        user=user, role__name=Role.RoleType.VIEWER, organization=organization
    ).exists()
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
    body = response.json()
    items = body.get("results", body) if isinstance(body, dict) else body
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
    from apps.core.signing import sign_request

    body = json.dumps({"status": Job.Status.RUNNING}).encode()
    signed = sign_request(body, "phase2-webhook-secret")
    response = api_client.generic(
        "POST",
        reverse("job-status-callback", kwargs={"job_id": job.id}),
        data=body,
        content_type="application/json",
        **{f"HTTP_{k.upper().replace('-', '_')}": v for k, v in signed.items()},
    )

    assert response.status_code == 200, response.content
    job.refresh_from_db()
    assert job.status == Job.Status.RUNNING


@pytest.mark.django_db
def test_supabase_profile_update_does_not_reactivate_suspended_user(api_client, user, monkeypatch):
    monkeypatch.setattr(settings, "SUPABASE_WEBHOOK_SIGNING_SECRET", "phase2-supabase-secret")
    user.is_active = False
    user.save(update_fields=["is_active"])
    payload = {
        "type": "UPDATE",
        "record": {
            "id": user.supabase_user_id,
            "email": "updated@example.com",
            "user_metadata": {"full_name": "Updated Profile"},
        },
    }
    body = json.dumps(payload).encode()

    response = api_client.generic(
        "POST",
        reverse("supabase-webhook"),
        data=body,
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer phase2-supabase-secret",
    )

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.is_active is False
    assert user.email == "updated@example.com"


@pytest.mark.django_db
def test_memberships_receive_owner_admin_and_member_viewer_roles(user):
    organization = Organization.objects.create(name="Lifecycle tenant", slug="lifecycle-tenant", owner=user)
    user.organizations.add(organization)
    member = User.objects.create_user(
        username="lifecycle-member",
        supabase_user_id="lifecycle-member",
        email="member@example.com",
    )
    membership = UserOrganization.objects.create(user=member, organization=organization)

    assert UserRole.objects.filter(
        user=user, organization=organization, role__name=Role.RoleType.ADMIN
    ).exists()
    assert UserRole.objects.filter(
        user=membership.user, organization=organization, role__name=Role.RoleType.VIEWER
    ).exists()


@pytest.mark.django_db
def test_collection_creation_uses_selected_tenant_and_requires_editor_role(api_client, user):
    owner = User.objects.create_user(
        username="collection-owner",
        supabase_user_id="collection-owner",
        email="collection-owner@example.com",
    )
    organization = Organization.objects.create(name="Knowledge tenant", slug="knowledge-tenant", owner=owner)
    owner.organizations.add(organization)
    user.organizations.add(organization)
    api_client.force_authenticate(user)
    payload = {"name": "Private knowledge"}

    blocked = api_client.post(
        reverse("collection-list"), payload, HTTP_X_ORGANIZATION_ID=str(organization.id)
    )
    assert blocked.status_code == 403

    editor = Role.objects.get(name=Role.RoleType.EDITOR)
    UserRole.objects.create(user=user, role=editor, organization=organization)
    allowed = api_client.post(
        reverse("collection-list"), payload, HTTP_X_ORGANIZATION_ID=str(organization.id)
    )

    assert allowed.status_code == 201, allowed.content
    assert allowed.json()["organizationId"] == str(organization.id)


@pytest.mark.django_db
def test_viewer_cannot_request_asset_upload_credentials(api_client, user):
    owner = User.objects.create_user(
        username="asset-owner", supabase_user_id="asset-owner", email="asset-owner@example.com"
    )
    organization = Organization.objects.create(name="Asset tenant", slug="asset-tenant", owner=owner)
    owner.organizations.add(organization)
    user.organizations.add(organization)
    api_client.force_authenticate(user)

    response = api_client.post(
        reverse("asset-signature"),
        {"originalFilename": "report.pdf", "bytes": 10},
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_prompt_slugs_are_unique_per_organization(api_model, user):
    first = Organization.objects.create(name="Slug tenant one", slug="slug-tenant-one", owner=user)
    second = Organization.objects.create(name="Slug tenant two", slug="slug-tenant-two", owner=user)
    user.organizations.add(first, second)

    first_prompt = Prompt.objects.create(
        name="Shared slug",
        slug="shared",
        category=Prompt.Category.TASK,
        content="one",
        organization=first,
        created_by=user,
    )
    second_prompt = Prompt.objects.create(
        name="Shared slug",
        slug="shared",
        category=Prompt.Category.TASK,
        content="two",
        organization=second,
        created_by=user,
    )

    assert first_prompt.slug == second_prompt.slug == "shared"


@pytest.mark.django_db
def test_evaluation_slugs_are_unique_per_organization(api_model, user):
    first = Organization.objects.create(
        name="Evaluation tenant one", slug="evaluation-tenant-one", owner=user
    )
    second = Organization.objects.create(
        name="Evaluation tenant two", slug="evaluation-tenant-two", owner=user
    )
    user.organizations.add(first, second)
    first_prompt = Prompt.objects.create(
        name="First prompt",
        slug="first-prompt",
        category=Prompt.Category.TASK,
        content="one",
        organization=first,
        created_by=user,
    )
    second_prompt = Prompt.objects.create(
        name="Second prompt",
        slug="second-prompt",
        category=Prompt.Category.TASK,
        content="two",
        organization=second,
        created_by=user,
    )

    first_evaluation = Evaluation.objects.create(
        name="Shared evaluation",
        slug="shared-evaluation",
        type=Evaluation.Type.ACCURACY,
        model=api_model,
        prompt=first_prompt,
        dataset_name="one",
        dataset_version="1",
        organization=first,
        created_by=user,
    )
    second_evaluation = Evaluation.objects.create(
        name="Shared evaluation",
        slug="shared-evaluation",
        type=Evaluation.Type.ACCURACY,
        model=api_model,
        prompt=second_prompt,
        dataset_name="two",
        dataset_version="1",
        organization=second,
        created_by=user,
    )

    assert first_evaluation.slug == second_evaluation.slug == "shared-evaluation"
