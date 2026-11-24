"""The organization audit log API (P16-4): admin-only reads, correct filters and a safe CSV export."""

from __future__ import annotations

import csv
import datetime as dt
import io
from unittest import mock

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.governance.models import AuditEvent
from apps.identity.models import Organization, User, UserOrganization


@pytest.fixture
def owner():
    return User.objects.create(
        username="audit-owner", supabase_user_id="sb-audit-owner", email="owner@example.com"
    )


@pytest.fixture
def org(owner):
    organization = Organization.objects.create(name="Audit workspace", owner=owner)
    UserOrganization.objects.create(user=owner, organization=organization)
    return organization


def _client(user, org) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    client.credentials(HTTP_X_ORGANIZATION_ID=str(org.id))
    return client


def _event(org, actor, *, at=None, **fields) -> AuditEvent:
    # The audit table is append-only (a trigger refuses UPDATE): set the time when the row is written.
    with mock.patch("django.utils.timezone.now", return_value=at or timezone.now()):
        return AuditEvent.objects.create(
            organization=org,
            actor=actor,
            category=fields.pop("category", AuditEvent.Category.CONFIGURATION),
            action=fields.pop("action", "api-key-list.post"),
            description=fields.pop("description", "Created an API key"),
            **fields,
        )


def _rows(response) -> list[dict]:
    data = response.json()
    return data["results"] if isinstance(data, dict) else data


@pytest.mark.django_db
def test_a_malformed_actor_filter_is_a_400_not_a_500(owner, org):
    response = _client(owner, org).get("/api/v1/audit-events/", {"actor": "not-a-uuid"})
    assert response.status_code == 400, response.content


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["start_date", "end_date"])
@pytest.mark.parametrize("value", ["2026-13-45", "yesterday", "2026-11-20T25:00:00"])
def test_impossible_dates_are_a_400(owner, org, name, value):
    assert _client(owner, org).get("/api/v1/audit-events/", {name: value}).status_code == 400


@pytest.mark.django_db
def test_an_end_date_includes_that_whole_day(owner, org):
    day = dt.datetime(2026, 11, 20, tzinfo=dt.UTC)
    _event(org, owner, at=day + dt.timedelta(hours=15), description="afternoon")
    _event(org, owner, at=day + dt.timedelta(days=1, hours=1), description="next day")
    query = {"start_date": "2026-11-20", "end_date": "2026-11-20"}
    rows = _rows(_client(owner, org).get("/api/v1/audit-events/", query))
    assert [row["description"] for row in rows] == ["afternoon"]
    assert rows[0]["outcome"] == "success"  # shown as "Result" in Settings → Audit log


@pytest.mark.django_db
def test_the_export_is_admin_only_tenant_scoped_and_neutralises_formulas(owner, org):
    member = User.objects.create(
        username="audit-member", supabase_user_id="sb-audit-member", email="m@example.com"
    )
    UserOrganization.objects.create(user=member, organization=org)
    other_owner = User.objects.create(username="other", supabase_user_id="sb-other", email="o@example.com")
    other = Organization.objects.create(name="Other", owner=other_owner)
    _event(other, other_owner, description="not yours")
    _event(org, owner, description='=HYPERLINK("https://evil.example","click")', user_agent="@SUM(1+1)")
    _event(org, owner, description="Plain text", at=timezone.now() - dt.timedelta(days=1))

    assert _client(member, org).get("/api/v1/audit-events/export/").status_code == 403

    response = _client(owner, org).get("/api/v1/audit-events/export/")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert "attachment" in response["Content-Disposition"]
    body = b"".join(response.streaming_content).decode() if response.streaming else response.content.decode()
    rows = list(csv.DictReader(io.StringIO(body)))
    assert [row["description"] for row in rows] == [
        '\'=HYPERLINK("https://evil.example","click")',
        "Plain text",
    ]
    assert rows[0]["user_agent"] == "'@SUM(1+1)"
    assert rows[0]["actor_email"] == "owner@example.com"
    assert "not yours" not in body


@pytest.mark.django_db
def test_the_export_follows_the_list_filters(owner, org):
    _event(
        org, owner, category=AuditEvent.Category.SECURITY, action="webhook.rejected", description="rejected"
    )
    _event(org, owner, description="configured")
    response = _client(owner, org).get("/api/v1/audit-events/export/", {"category": "security"})
    body = b"".join(response.streaming_content).decode() if response.streaming else response.content.decode()
    rows = list(csv.DictReader(io.StringIO(body)))
    assert [row["description"] for row in rows] == ["rejected"]
