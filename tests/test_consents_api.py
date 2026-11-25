"""Consent records (P16-5): personal, never deleted, and terms/privacy are not withdrawn through the API."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.governance.models import ConsentRecord
from apps.identity.models import Organization, Role, User, UserOrganization, UserRole


@pytest.fixture
def owner():
    return User.objects.create(username="c-owner", supabase_user_id="sb-c-owner", email="owner@example.com")


@pytest.fixture
def org(owner):
    organization = Organization.objects.create(name="Consent workspace", owner=owner)
    UserOrganization.objects.create(user=owner, organization=organization)
    return organization


def _member(org, name, role=None):
    user = User.objects.create(username=name, supabase_user_id=f"sb-{name}", email=f"{name}@example.com")
    UserOrganization.objects.create(user=user, organization=org)
    if role:
        UserRole.objects.create(user=user, organization=org, role=Role.objects.get_or_create(name=role)[0])
    return user


def _client(user, org) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    client.credentials(HTTP_X_ORGANIZATION_ID=str(org.id))
    return client


def _consent(user, org, consent_type=ConsentRecord.ConsentType.TERMS) -> ConsentRecord:
    return ConsentRecord.objects.create(
        user=user,
        organization=org,
        consent_type=consent_type,
        status=ConsentRecord.Status.GRANTED,
        version="2026-11",
    )


def _rows(response) -> list[dict]:
    data = response.json()
    return data["results"] if isinstance(data, dict) else data


@pytest.mark.django_db
def test_every_member_sees_only_their_own_consents(owner, org):
    viewer = _member(org, "viewer")
    editor = _member(org, "editor", Role.RoleType.EDITOR)
    mine = _consent(viewer, org)
    theirs = _consent(editor, org)

    response = _client(viewer, org).get("/api/v1/consents/")
    assert response.status_code == 200, response.content  # a viewer may read their own records
    assert [row["id"] for row in _rows(response)] == [str(mine.id)]

    # An editor (or the owner) cannot read, change or delete another member's consent.
    for user in (editor, owner):
        client = _client(user, org)
        assert client.get(f"/api/v1/consents/{mine.id}/").status_code == 404
        assert (
            client.patch(f"/api/v1/consents/{mine.id}/", {"status": "withdrawn"}, format="json").status_code
            == 404
        )
    mine.refresh_from_db()
    assert mine.status == ConsentRecord.Status.GRANTED
    assert {row["id"] for row in _rows(_client(editor, org).get("/api/v1/consents/"))} == {str(theirs.id)}


@pytest.mark.django_db
def test_consent_records_are_never_deleted(owner, org):
    record = _consent(owner, org, ConsentRecord.ConsentType.MARKETING)
    response = _client(owner, org).delete(f"/api/v1/consents/{record.id}/")
    assert response.status_code == 405
    assert ConsentRecord.objects.filter(id=record.id).exists()


@pytest.mark.django_db
def test_terms_and_privacy_are_not_withdrawn_through_the_api(owner, org):
    terms = _consent(owner, org, ConsentRecord.ConsentType.TERMS)
    response = _client(owner, org).patch(
        f"/api/v1/consents/{terms.id}/", {"status": "withdrawn"}, format="json"
    )
    assert response.status_code == 409, response.content
    terms.refresh_from_db()
    assert terms.status == ConsentRecord.Status.GRANTED


@pytest.mark.django_db
def test_an_optional_consent_is_withdrawn_with_its_time(owner, org):
    marketing = _consent(owner, org, ConsentRecord.ConsentType.MARKETING)
    response = _client(owner, org).patch(
        f"/api/v1/consents/{marketing.id}/", {"status": "withdrawn"}, format="json"
    )
    assert response.status_code == 200, response.content
    marketing.refresh_from_db()
    assert marketing.status == ConsentRecord.Status.WITHDRAWN and marketing.withdrawn_at is not None


@pytest.mark.django_db
def test_the_server_records_where_a_consent_came_from(owner, org):
    response = _client(owner, org).post(
        "/api/v1/consents/",
        {
            "consent_type": "marketing",
            "status": "granted",
            "version": "2026-11",
            "ip_address": "198.51.100.1",
        },
        format="json",
        REMOTE_ADDR="203.0.113.9",
        HTTP_USER_AGENT="probe-agent",
    )
    assert response.status_code == 201, response.content
    record = ConsentRecord.objects.get(user=owner, consent_type="marketing")
    assert record.ip_address == "203.0.113.9" and record.user_agent == "probe-agent"
    assert record.granted_at is not None
    # Terms and privacy are recorded at sign-up, not created through this endpoint.
    terms = _client(owner, org).post(
        "/api/v1/consents/", {"consent_type": "terms", "status": "granted", "version": "x"}, format="json"
    )
    assert terms.status_code == 400
