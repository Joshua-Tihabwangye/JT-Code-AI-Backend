"""Team management API used by the Settings → Team tab (backlog Phase 7).

Members carry a name and an owner flag (P7-3), pending invites are listed and can be revoked (P7-6),
and users can list the workspaces they belong to for the workspace switcher (P7-5).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.identity.models import (
    Organization,
    OrganizationInvite,
    Role,
    User,
    UserOrganization,
    UserRole,
    hash_invite_token,
)

pytestmark = pytest.mark.django_db


def _client(user: User, organization: Organization | None = None) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    if organization is not None:
        client.credentials(HTTP_X_ORGANIZATION_ID=str(organization.id))
    return client


def _give_role(user: User, organization: Organization, role_name: str) -> None:
    role, _ = Role.objects.get_or_create(name=role_name)
    UserRole.objects.create(user=user, organization=organization, role=role)


@pytest.fixture
def owner():
    return User.objects.create(
        username="owner",
        supabase_user_id="sb-owner",
        email="owner@example.com",
        first_name="Ada",
        last_name="Lovelace",
        full_name="Ada Lovelace",
    )


@pytest.fixture
def team(owner):
    organization = Organization.objects.create(name="Analytical Engines", owner=owner)
    UserOrganization.objects.create(user=owner, organization=organization)
    return organization


@pytest.fixture
def editor(team):
    user = User.objects.create(
        username="editor", supabase_user_id="sb-editor", email="editor@example.com", first_name="Charles"
    )
    UserOrganization.objects.create(user=user, organization=team)
    _give_role(user, team, Role.RoleType.EDITOR)
    return user


# P7-3 members payload ------------------------------------------------------------------------------


def test_members_carry_name_role_and_owner_flag(owner, team, editor):
    response = _client(owner, team).get(reverse("organization-members"))
    assert response.status_code == 200, response.content
    members = {member["email"]: member for member in response.json()["members"]}
    assert members["owner@example.com"] == {
        "user_id": str(owner.id),
        "email": "owner@example.com",
        "name": "Ada Lovelace",
        "role": "admin",
        "is_owner": True,
    }
    assert members["editor@example.com"]["name"] == "Charles"
    assert members["editor@example.com"]["role"] == "editor"
    assert members["editor@example.com"]["is_owner"] is False


def test_members_list_is_admin_only(team, editor):
    assert _client(editor, team).get(reverse("organization-members")).status_code == 403


def test_role_change_returns_the_full_member_payload(owner, team, editor):
    url = reverse("organization-member-detail", args=[editor.id])
    response = _client(owner, team).patch(url, {"role": "viewer"}, format="json")
    assert response.status_code == 200, response.content
    assert response.json() == {
        "user_id": str(editor.id),
        "email": "editor@example.com",
        "name": "Charles",
        "role": "viewer",
        "is_owner": False,
    }


def test_owner_cannot_be_changed_or_removed(owner, team):
    client = _client(owner, team)
    url = reverse("organization-member-detail", args=[owner.id])
    assert client.patch(url, {"role": "viewer"}, format="json").status_code == 400
    assert client.delete(url).status_code == 400
    assert UserOrganization.objects.filter(user=owner, organization=team).exists()


# P7-6 pending invites: listed, never re-exposed, revocable ------------------------------------------


def test_pending_invites_are_listed_without_their_token(owner, team):
    client = _client(owner, team)
    created = client.post(reverse("organization-members"), {"email": "Guest@Example.com", "role": "editor"})
    assert created.status_code == 201, created.content
    assert created.json()["token"]
    OrganizationInvite.objects.create(
        organization=team,
        email="late@example.com",
        role="viewer",
        token=hash_invite_token("expired"),
        expires_at=timezone.now() - timedelta(minutes=1),
    )

    listed = client.get(reverse("organization-members")).json()["invites"]
    assert [invite["email"] for invite in listed] == ["guest@example.com"]  # the expired one is hidden
    assert set(listed[0]) == {"id", "email", "role", "expires_at"}


def test_a_second_pending_invite_for_the_same_email_is_refused(owner, team):
    client = _client(owner, team)
    url = reverse("organization-members")
    assert client.post(url, {"email": "guest@example.com", "role": "viewer"}).status_code == 201
    assert client.post(url, {"email": "GUEST@example.com", "role": "admin"}).status_code == 409


def test_revoked_invite_link_stops_working(owner, team):
    created = _client(owner, team).post(
        reverse("organization-members"), {"email": "guest@example.com", "role": "editor"}
    )
    invite_id, raw_token = created.json()["id"], created.json()["token"]

    revoke = _client(owner, team).delete(reverse("organization-invite-detail", args=[invite_id]))
    assert revoke.status_code == 204
    assert OrganizationInvite.objects.get(id=invite_id).status == OrganizationInvite.Status.REVOKED

    guest = User.objects.create(username="guest", supabase_user_id="sb-guest", email="guest@example.com")
    accept = _client(guest).post(
        reverse("organization-invite-accept", args=[invite_id]), {"token": raw_token}, format="json"
    )
    assert accept.status_code == 404
    assert not UserOrganization.objects.filter(user=guest, organization=team).exists()
    # Revoking again (or an unknown id) is a 404, not a silent success.
    again = _client(owner, team).delete(reverse("organization-invite-detail", args=[invite_id]))
    assert again.status_code == 404


def test_only_admins_of_the_inviting_workspace_can_revoke(owner, team, editor):
    invite = OrganizationInvite.objects.create(
        organization=team,
        email="guest@example.com",
        role="viewer",
        token=hash_invite_token("t"),
        expires_at=timezone.now() + timedelta(days=1),
    )
    url = reverse("organization-invite-detail", args=[invite.id])
    assert _client(editor, team).delete(url).status_code == 403

    # An admin of another workspace cannot reach this workspace's invite.
    stranger = User.objects.create(username="stranger", supabase_user_id="sb-stranger", email="s@example.com")
    other = Organization.objects.create(name="Elsewhere", owner=stranger)
    UserOrganization.objects.create(user=stranger, organization=other)
    assert _client(stranger, other).delete(url).status_code == 404
    assert _client(stranger, team).delete(url).status_code == 403  # not a member of `team`
    invite.refresh_from_db()
    assert invite.status == OrganizationInvite.Status.PENDING


def test_accept_returns_the_member_and_the_joined_workspace(owner, team):
    created = _client(owner, team).post(
        reverse("organization-members"), {"email": "guest@example.com", "role": "editor"}
    )
    guest = User.objects.create(
        username="guest", supabase_user_id="sb-guest", email="guest@example.com", full_name="Grace Hopper"
    )
    accept = _client(guest).post(
        reverse("organization-invite-accept", args=[created.json()["id"]]),
        {"token": created.json()["token"]},
        format="json",
    )
    assert accept.status_code == 200, accept.content
    assert accept.json() == {
        "user_id": str(guest.id),
        "email": "guest@example.com",
        "name": "Grace Hopper",
        "role": "editor",
        "is_owner": False,
        "organization": {"id": str(team.id), "name": "Analytical Engines"},
    }


# P7-5 workspace list for the switcher -------------------------------------------------------------


def test_workspace_list_shows_each_membership_with_its_role(owner, team, editor):
    own = Organization.objects.create(name="Charles's workspace", owner=editor)
    UserOrganization.objects.create(user=editor, organization=own)

    response = _client(editor).get(reverse("organization-list"))
    assert response.status_code == 200, response.content
    workspaces = {item["name"]: item for item in response.json()["organizations"]}
    assert set(workspaces) == {"Analytical Engines", "Charles's workspace"}
    assert workspaces["Analytical Engines"] == {
        "id": str(team.id),
        "name": "Analytical Engines",
        "role": "editor",
        "is_owner": False,
        "is_default": True,  # the earliest membership is the default without X-Organization-ID
    }
    assert workspaces["Charles's workspace"]["role"] == "admin"
    assert workspaces["Charles's workspace"]["is_owner"] is True
    assert workspaces["Charles's workspace"]["is_default"] is False


def test_workspace_list_never_shows_other_users_workspaces(owner, team):
    stranger = User.objects.create(username="stranger", supabase_user_id="sb-stranger", email="s@example.com")
    assert _client(stranger).get(reverse("organization-list")).json() == {"organizations": []}
    assert APIClient().get(reverse("organization-list")).status_code in {401, 403}


# P9-2 the invite row is locked while it is accepted ------------------------------------------------


def test_an_accepted_invite_cannot_be_accepted_again(owner, team):
    created = _client(owner, team).post(
        reverse("organization-members"), {"email": "guest@example.com", "role": "viewer"}
    )
    url = reverse("organization-invite-accept", args=[created.json()["id"]])
    guest = User.objects.create(username="guest", supabase_user_id="sb-guest", email="guest@example.com")
    body = {"token": created.json()["token"]}

    assert _client(guest).post(url, body, format="json").status_code == 200
    # A replay of the same link (or a request that waited on the row lock) finds it no longer pending.
    assert _client(guest).post(url, body, format="json").status_code == 404
    assert UserRole.objects.filter(user=guest, organization=team).count() == 1


def test_a_non_ascii_invite_token_is_refused_not_a_server_error(owner, team):
    created = _client(owner, team).post(
        reverse("organization-members"), {"email": "guest@example.com", "role": "viewer"}
    )
    guest = User.objects.create(username="guest", supabase_user_id="sb-guest", email="guest@example.com")
    response = _client(guest).post(
        reverse("organization-invite-accept", args=[created.json()["id"]]), {"token": "tökén"}, format="json"
    )
    assert response.status_code == 403
