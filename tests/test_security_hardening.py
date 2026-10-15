"""Regression tests for the backend security audit fixes (tenant isolation, roles, secrets)."""

from __future__ import annotations

import time
import uuid
from decimal import Decimal

import jwt as pyjwt
import pytest
from django.conf import settings
from rest_framework.test import APIClient

from apps.billing.models import CreditWallet
from apps.identity.models import Organization, OrganizationInvite, Role, User, UserOrganization, UserRole


def _member(org: Organization, label: str, role: str) -> User:
    user = User.objects.create_user(
        username=f"{label}-{role}",
        supabase_user_id=f"sb-{label}-{role}",
        email=f"{label}-{role}@example.test",
    )
    UserOrganization.objects.get_or_create(user=user, organization=org)
    UserRole.objects.filter(user=user, organization=org).delete()
    role_obj, _ = Role.objects.get_or_create(name=role)
    UserRole.objects.create(user=user, organization=org, role=role_obj)
    return user


def _org(label: str) -> tuple[Organization, User]:
    owner = User.objects.create_user(
        username=f"{label}-owner", supabase_user_id=f"sb-{label}-owner", email=f"{label}@example.test"
    )
    org = Organization.objects.create(name=f"{label} org", slug=f"{label}-org", owner=owner)
    UserOrganization.objects.get_or_create(user=owner, organization=org)
    CreditWallet.objects.update_or_create(organization=org, defaults={"balance": Decimal("1000")})
    return org, owner


def _client(user: User, org: Organization) -> APIClient:
    api = APIClient()
    api.force_authenticate(user)
    api.credentials(HTTP_X_ORGANIZATION_ID=str(org.id))
    return api


def _token_client(sub: str, email: str, metadata: dict | None = None) -> APIClient:
    claims = {
        "sub": sub,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "user_metadata": metadata or {},
    }
    api = APIClient()
    token = pyjwt.encode(claims, settings.SUPABASE_JWT_SECRET, algorithm="HS256")
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return api


# H1 ---------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_viewer_cannot_change_or_disconnect_an_integration():
    from apps.integrations.facade import connector_for
    from apps.integrations.models import ConnectorAccount

    org, owner = _org("h1")
    account = ConnectorAccount.objects.create(
        organization=org, connector=connector_for("github"), user=owner, metadata={"config": {}}
    )
    viewer = _client(_member(org, "h1", Role.RoleType.VIEWER), org)

    assert (
        viewer.patch(f"/api/v1/integrations/{account.id}/", {"displayName": "x"}, format="json").status_code
        == 403
    )
    assert viewer.delete(f"/api/v1/integrations/{account.id}/").status_code == 403
    assert viewer.post("/api/v1/integrations/connect/", {"key": "github"}, format="json").status_code == 403
    account.refresh_from_db()
    assert account.status != ConnectorAccount.Status.REVOKED


# H2 ---------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_payload_cannot_move_records_into_another_tenant():
    from apps.integrations.facade import connector_for
    from apps.integrations.models import ConnectorAccount

    org_a, owner_a = _org("h2a")
    org_b, _ = _org("h2b")
    account = ConnectorAccount.objects.create(
        organization=org_a, connector=connector_for("github"), user=owner_a
    )
    api = _client(owner_a, org_a)

    api.patch(f"/api/v1/connector-accounts/{account.id}/", {"organization": str(org_b.id)}, format="json")
    account.refresh_from_db()
    assert account.organization_id == org_a.id

    created = api.post(
        "/api/v1/safety-events/",
        {"organization": str(org_b.id), "category": "other", "severity": "low", "description": "x"},
        format="json",
    )
    if created.status_code == 201:
        from apps.governance.models import SafetyEvent

        event = SafetyEvent.objects.get(id=created.json()["id"])
        assert event.organization_id == org_a.id
        assert event.user_id == owner_a.id
    assert (
        not __import__("apps.governance.models", fromlist=["SafetyEvent"])
        .SafetyEvent.objects.filter(organization=org_b)
        .exists()
    )


# H3 ---------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_editors_cannot_manage_retention_and_audit_logs_keep_their_legal_floor():
    org, owner = _org("h3")
    editor = _client(_member(org, "h3", Role.RoleType.EDITOR), org)
    purge = {
        "data_category": "audit_events",
        "action": "hard_delete",
        "retention_days": 0,
        "grace_period_days": 0,
    }

    assert editor.post("/api/v1/retention-rules/", purge, format="json").status_code == 403
    admin = _client(owner, org)
    response = admin.post("/api/v1/retention-rules/", purge, format="json")
    assert response.status_code == 400
    assert "retention_days" in str(response.json())


# M1 ---------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_chat_requests_of_other_members_are_invisible():
    from apps.conversations.models import ChatRequest, Conversation

    org, owner = _org("m1")
    colleague = _member(org, "m1", Role.RoleType.EDITOR)
    conversation = Conversation.objects.create(owner=owner, organization=org, title="Private")
    request = ChatRequest.objects.create(
        owner=owner, organization=org, conversation=conversation, idempotency_key="m1", input_text="secret"
    )
    api = _client(colleague, org)

    assert api.get(f"/api/v1/chat/requests/{request.id}/").status_code == 404
    assert api.get(f"/api/v1/conversations/{conversation.id}/export/").status_code == 404
    created = api.post(
        "/api/v1/chat/requests/",
        {"conversationId": str(conversation.id), "chatInput": "hello"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="m1-colleague",
    )
    assert created.status_code == 404


# M2 ---------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_invite_acceptance_requires_the_token_and_profile_email_is_read_only():
    org, _ = _org("m2")
    invite = OrganizationInvite.objects.create(
        organization=org, email="invitee@example.test", role="viewer", token=uuid.uuid4().hex
    )
    invitee = User.objects.create_user(
        username="invitee", supabase_user_id="sb-invitee", email="invitee@example.test"
    )
    api = APIClient()
    api.force_authenticate(invitee)

    assert (
        api.post(f"/api/v1/organizations/invites/{invite.id}/accept/", {}, format="json").status_code == 403
    )
    wrong = api.post(f"/api/v1/organizations/invites/{invite.id}/accept/", {"token": "nope"}, format="json")
    assert wrong.status_code == 403
    ok = api.post(
        f"/api/v1/organizations/invites/{invite.id}/accept/", {"token": invite.token}, format="json"
    )
    assert ok.status_code == 200

    api.patch("/api/v1/settings/profile/", {"email": "victim@example.test"}, format="json")
    invitee.refresh_from_db()
    assert invitee.email == "invitee@example.test"


# M3 ---------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_billing_changes_require_an_admin():
    org, owner = _org("m3")
    editor = _client(_member(org, "m3", Role.RoleType.EDITOR), org)

    assert editor.patch("/api/v1/wallets/me/", {"autoTopupEnabled": True}, format="json").status_code == 403
    assert editor.post("/api/v1/subscriptions/cancel/").status_code == 403
    assert _client(owner, org).get("/api/v1/wallets/me/").status_code == 200


# M4 ---------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_inbound_webhook_secret_is_never_listed():
    from apps.integrations.models import Webhook

    org, owner = _org("m4")
    Webhook.objects.create(
        organization=org, name="hook", url="https://hooks.example.test/x", secret="s3cret-value"
    )
    viewer = _client(_member(org, "m4", Role.RoleType.VIEWER), org)

    body = viewer.get("/api/v1/webhooks/").content.decode()
    assert "s3cret-value" not in body


# L4 ---------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_only_the_creator_or_an_admin_deletes_a_knowledge_collection():
    from apps.knowledge.models import Collection

    org, owner = _org("l4")
    collection = Collection.objects.create(
        organization=org, name="kb", created_by=owner, embedding_model="echo"
    )
    editor = _client(_member(org, "l4", Role.RoleType.EDITOR), org)

    assert editor.delete(f"/api/v1/knowledge/collections/{collection.id}/").status_code == 403
    assert Collection.objects.filter(id=collection.id).exists()
    assert _client(owner, org).delete(f"/api/v1/knowledge/collections/{collection.id}/").status_code == 204


# First sign-in provisioning --------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.unfunded
def test_first_sign_in_creates_a_personal_workspace_and_profile(settings):
    from apps.billing.models import Plan

    settings.BILLING_DEFAULT_PLAN = "free"
    free = Plan.objects.get(slug="free")
    api = _token_client(
        "sb-new-user",
        "new@example.test",
        {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "country": "UG",
            "countryName": "Uganda",
            "dialCode": "+256",
        },
    )

    account = api.get("/api/v1/accounts/me/")
    assert account.status_code == 200, account.content
    body = account.json()
    assert (body["firstName"], body["lastName"], body["countryCode"]) == ("Ada", "Lovelace", "UG")

    user = User.objects.get(supabase_user_id="sb-new-user")
    memberships = UserOrganization.objects.filter(user=user)
    assert memberships.count() == 1
    org = memberships.get().organization
    assert org.owner_id == user.id
    assert CreditWallet.objects.get(organization=org).balance == free.monthly_credits
    # Tenant APIs work immediately, and a second request provisions nothing new.
    assert api.get("/api/v1/conversations/").status_code == 200
    assert api.get("/api/v1/wallets/me/").status_code == 200
    assert UserOrganization.objects.filter(user=user).count() == 1


# Avatars ----------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_avatar_upload_rejects_non_images_and_spoofed_types():
    from django.core.files.uploadedfile import SimpleUploadedFile

    org, owner = _org("avatar")
    api = _client(owner, org)

    svg = SimpleUploadedFile("a.svg", b"<svg onload=alert(1)>", content_type="image/svg+xml")
    assert api.post("/api/v1/accounts/me/avatar/", {"file": svg}, format="multipart").status_code == 400
    fake_png = SimpleUploadedFile("a.png", b"<html>not a png</html>", content_type="image/png")
    assert api.post("/api/v1/accounts/me/avatar/", {"file": fake_png}, format="multipart").status_code == 400
    big = SimpleUploadedFile(
        "a.png", b"\x89PNG\r\n\x1a\n" + b"0" * (3 * 1024 * 1024), content_type="image/png"
    )
    assert api.post("/api/v1/accounts/me/avatar/", {"file": big}, format="multipart").status_code == 413


# Connection management -------------------------------------------------------------------------


def test_asgi_processes_never_keep_persistent_database_connections(monkeypatch):
    from config.settings.base import runtime_connection_settings

    url = "postgresql://user:pass@127.0.0.1:5432/db"
    monkeypatch.setenv("DATABASE_CONN_MAX_AGE", "60")
    monkeypatch.setenv("DATABASE_POOLER_MODE", "direct")
    monkeypatch.delenv("JT_CODE_SERVER_INTERFACE", raising=False)
    assert (
        runtime_connection_settings(database_url=url)[0]["default"]["CONN_MAX_AGE"] == 60
    )  # Celery, commands

    monkeypatch.setenv("JT_CODE_SERVER_INTERFACE", "asgi")
    assert runtime_connection_settings(database_url=url)[0]["default"]["CONN_MAX_AGE"] == 0
