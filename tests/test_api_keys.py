"""Organization API keys (P16-1): issued, accepted with X-API-Key, and every limit fails closed."""

from __future__ import annotations

import datetime as dt
import hashlib

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.conversations.models import Conversation
from apps.identity.models import Organization, Role, User, UserOrganization, UserRole
from apps.integrations.models import APIKey


@pytest.fixture
def owner():
    return User.objects.create(
        username="key-owner", supabase_user_id="sb-key-owner", email="owner@example.com"
    )


@pytest.fixture
def org(owner):
    organization = Organization.objects.create(name="Key workspace", owner=owner)
    UserOrganization.objects.create(user=owner, organization=organization)
    return organization


@pytest.fixture
def editor(org):
    user = User.objects.create(
        username="key-editor", supabase_user_id="sb-key-editor", email="ed@example.com"
    )
    UserOrganization.objects.create(user=user, organization=org)
    role, _ = Role.objects.get_or_create(name=Role.RoleType.EDITOR)
    UserRole.objects.create(user=user, organization=org, role=role)
    return user


def _session(user: User, org: Organization) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    client.credentials(HTTP_X_ORGANIZATION_ID=str(org.id))
    return client


def _create(user, org, **body):
    response = _session(user, org).post(
        "/api/v1/api-keys/", {"name": "CI", "scopes": ["read"], **body}, format="json"
    )
    assert response.status_code == 201, response.content
    return response.json()


def _keyed(key: str, **headers) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=key, **headers)
    return client


@pytest.mark.django_db
def test_the_key_is_shown_once_and_only_its_digest_is_stored(owner, org):
    created = _create(owner, org)
    key = created["key"]
    assert key.startswith(created["prefix"] + "_") and len(key) > 40
    stored = APIKey.objects.get(id=created["id"])
    assert stored.key_hash == hashlib.sha256(key.encode()).hexdigest()

    listed = _session(owner, org).get("/api/v1/api-keys/").json()
    rows = listed["results"] if isinstance(listed, dict) else listed
    assert rows and all("key" not in row and "full_key" not in row and "key_hash" not in row for row in rows)
    assert stored.key_hash not in str(listed)


@pytest.mark.django_db
def test_a_read_key_reads_its_workspace_and_cannot_write(owner, org):
    Conversation.objects.create(owner=owner, organization=org, title="Visible to the key")
    key = _create(owner, org, scopes=["read"])["key"]

    response = _keyed(key).get("/api/v1/conversations/")
    assert response.status_code == 200, response.content
    assert "Visible to the key" in str(response.content)

    refused = _keyed(key).post("/api/v1/conversations/", {"title": "No"}, format="json")
    assert refused.status_code == 403
    assert not Conversation.objects.filter(title="No").exists()


@pytest.mark.django_db
def test_a_write_key_writes_into_the_key_workspace_only(owner, org):
    other = Organization.objects.create(name="Owner's other workspace", owner=owner)
    UserOrganization.objects.create(user=owner, organization=other)
    key = _create(owner, org, scopes=["read", "write"])["key"]

    created = _keyed(key).post("/api/v1/conversations/", {"title": "From a server"}, format="json")
    assert created.status_code == 201, created.content
    assert Conversation.objects.get(title="From a server").organization_id == org.id

    # Naming another workspace, even one the user belongs to, is refused: the key is pinned.
    pinned = _keyed(key, HTTP_X_ORGANIZATION_ID=str(other.id)).get("/api/v1/conversations/")
    assert pinned.status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/api-keys/",
        "/api/v1/accounts/me/",
        "/api/v1/settings/account/",
        "/api/v1/organizations/members/",
    ],
)
def test_a_key_cannot_manage_keys_accounts_or_members(owner, org, path):
    key = _create(owner, org, scopes=["read", "write"])["key"]
    assert _keyed(key).get(path).status_code == 403
    assert _keyed(key).post(path, {"name": "persist", "scopes": ["write"]}, format="json").status_code == 403
    assert APIKey.objects.count() == 1


@pytest.mark.django_db
def test_revoked_expired_unknown_and_orphaned_keys_are_refused(owner, org, editor):
    revoked = _create(owner, org)
    assert (
        _session(owner, org).post(f"/api/v1/api-keys/{revoked['id']}/revoke/").json()["status"] == "revoked"
    )
    assert _keyed(revoked["key"]).get("/api/v1/conversations/").status_code == 401

    expired = _create(owner, org)
    APIKey.objects.filter(id=expired["id"]).update(expires_at=timezone.now() - dt.timedelta(minutes=1))
    assert _keyed(expired["key"]).get("/api/v1/conversations/").status_code == 401
    assert APIKey.objects.get(id=expired["id"]).status == APIKey.Status.EXPIRED

    assert _keyed("jtk_live_" + "x" * 43).get("/api/v1/conversations/").status_code == 401

    # A member who leaves the workspace takes no access with them.
    leaver = _create(editor, org)
    UserOrganization.objects.filter(user=editor, organization=org).delete()
    assert _keyed(leaver["key"]).get("/api/v1/conversations/").status_code == 403


@pytest.mark.django_db
def test_a_revoked_key_cannot_be_reactivated_and_only_the_name_changes(owner, org):
    created = _create(owner, org)
    session = _session(owner, org)
    session.post(f"/api/v1/api-keys/{created['id']}/revoke/")
    session.patch(
        f"/api/v1/api-keys/{created['id']}/",
        {"status": "active", "scopes": ["read", "write"], "rate_limit": 999999, "name": "Renamed"},
        format="json",
    )
    stored = APIKey.objects.get(id=created["id"])
    assert (stored.status, stored.scopes, stored.name) == (APIKey.Status.REVOKED, ["read"], "Renamed")
    assert stored.rate_limit != 999999
    assert session.put(f"/api/v1/api-keys/{created['id']}/", {"name": "x"}, format="json").status_code == 405


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body",
    [
        {"scopes": ["admin"]},
        {"scopes": []},
        {"rate_limit": 10**9},
        {"expires_at": "2000-01-01T00:00:00Z"},
    ],
)
def test_invalid_scopes_limits_and_expiry_are_refused(owner, org, body):
    response = _session(owner, org).post(
        "/api/v1/api-keys/", {"name": "Bad", "scopes": ["read"], **body}, format="json"
    )
    assert response.status_code == 400, response.content
    assert not APIKey.objects.exists()


@pytest.mark.django_db
def test_a_key_without_scopes_is_read_only(owner, org):
    response = _session(owner, org).post("/api/v1/api-keys/", {"name": "Default"}, format="json")
    assert response.status_code == 201, response.content
    assert response.json()["scopes"] == ["read"]


@pytest.mark.django_db
def test_members_see_their_own_keys_and_admins_see_all(owner, org, editor):
    _create(owner, org, name="Owner key")
    editor_key = _create(editor, org, name="Editor key")

    def names(user):
        data = _session(user, org).get("/api/v1/api-keys/").json()
        return {row["name"] for row in (data["results"] if isinstance(data, dict) else data)}

    assert names(editor) == {"Editor key"}
    assert names(owner) == {"Owner key", "Editor key"}
    # The owner (admin) can revoke a member's key.
    assert _session(owner, org).post(f"/api/v1/api-keys/{editor_key['id']}/revoke/").status_code == 200


@pytest.mark.django_db
def test_each_key_has_its_own_hourly_limit(owner, org):
    key = _create(owner, org, rate_limit=2)["key"]
    client = _keyed(key)
    assert [client.get("/api/v1/conversations/").status_code for _ in range(3)] == [200, 200, 429]
