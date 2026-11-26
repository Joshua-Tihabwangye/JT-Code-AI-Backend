"""Support cases (P16-3): customers write in the app, support answers, both sides are notified."""

from __future__ import annotations

import pytest
from django.core import mail
from rest_framework.test import APIClient

from apps.governance.models import SupportCase, SupportCaseMessage
from apps.governance.support import add_message
from apps.identity.models import Organization, User, UserOrganization


@pytest.fixture
def owner():
    return User.objects.create(
        username="case-owner", supabase_user_id="sb-case-owner", email="owner@example.com"
    )


@pytest.fixture
def org(owner):
    organization = Organization.objects.create(name="Case workspace", owner=owner)
    UserOrganization.objects.create(user=owner, organization=organization)
    return organization


@pytest.fixture
def member(org):
    user = User.objects.create(
        username="case-member", supabase_user_id="sb-case-member", email="member@example.com"
    )
    UserOrganization.objects.create(user=user, organization=org)  # a viewer by default
    return user


@pytest.fixture(autouse=True)
def support_inbox(settings):
    settings.SUPPORT_NOTIFICATION_EMAIL = "support@jt-code.example"
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


def _client(user, org) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    client.credentials(HTTP_X_ORGANIZATION_ID=str(org.id))
    return client


def _open(user, org, captured, **body):
    with captured(execute=True):
        response = _client(user, org).post(
            "/api/v1/support-cases/",
            {
                "subject": "Export fails",
                "description": "The PDF export spins forever.",
                "category": "bug",
                **body,
            },
            format="json",
        )
    assert response.status_code == 201, response.content
    return response.json()


@pytest.mark.django_db
def test_any_member_opens_a_case_and_support_is_mailed(member, org, django_capture_on_commit_callbacks):
    case = _open(member, org, django_capture_on_commit_callbacks)
    assert (
        case["status"] == "open"
        and case["priority"] == "medium"
        and case["user_email"] == "member@example.com"
    )
    (notice,) = mail.outbox
    assert notice.to == ["support@jt-code.example"]
    assert "Export fails" in notice.subject and "PDF export spins" in notice.body
    assert "member@example.com" in notice.body


@pytest.mark.django_db
def test_no_mail_without_a_support_inbox(member, org, settings, django_capture_on_commit_callbacks):
    settings.SUPPORT_NOTIFICATION_EMAIL = ""
    _open(member, org, django_capture_on_commit_callbacks)
    assert mail.outbox == []


@pytest.mark.django_db
def test_members_see_their_own_cases_admins_all_and_other_workspaces_none(
    owner, member, org, django_capture_on_commit_callbacks
):
    mine = _open(member, org, django_capture_on_commit_callbacks, subject="Mine")
    _open(owner, org, django_capture_on_commit_callbacks, subject="Owner's")

    def subjects(user):
        data = _client(user, org).get("/api/v1/support-cases/").json()
        return {row["subject"] for row in (data["results"] if isinstance(data, dict) else data)}

    assert subjects(member) == {"Mine"}
    assert subjects(owner) == {"Mine", "Owner's"}

    stranger = User.objects.create(username="stranger", supabase_user_id="sb-stranger", email="s@example.com")
    elsewhere = Organization.objects.create(name="Elsewhere", owner=stranger)
    UserOrganization.objects.create(user=stranger, organization=elsewhere)
    outsider = _client(stranger, elsewhere)
    assert outsider.get(f"/api/v1/support-cases/{mine['id']}/").status_code == 404
    reply = outsider.post(f"/api/v1/support-cases/{mine['id']}/messages/", {"body": "hi"}, format="json")
    assert reply.status_code == 404


@pytest.mark.django_db
def test_support_replies_by_mail_and_a_customer_reply_reopens(
    member, org, django_capture_on_commit_callbacks
):
    case_id = _open(member, org, django_capture_on_commit_callbacks)["id"]
    case = SupportCase.objects.get(id=case_id)
    staff = User.objects.create(
        username="agent", supabase_user_id="sb-agent", email="agent@jt-code.example", is_staff=True
    )
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        add_message(case, staff, SupportCaseMessage.Author.SUPPORT, "Fixed in today's release.")
    case.refresh_from_db()
    assert case.status == SupportCase.Status.WAITING_USER and case.first_response_at is not None
    (to_customer,) = mail.outbox
    assert to_customer.to == ["member@example.com"] and "Fixed in today's release." in to_customer.body

    case.status = SupportCase.Status.RESOLVED
    case.save(update_fields=["status"])
    mail.outbox.clear()
    client = _client(member, org)
    with django_capture_on_commit_callbacks(execute=True):
        reply = client.post(
            f"/api/v1/support-cases/{case_id}/messages/", {"body": "Still broken."}, format="json"
        )
    assert reply.status_code == 201
    case.refresh_from_db()
    assert case.status == SupportCase.Status.OPEN
    assert mail.outbox[0].to == ["support@jt-code.example"] and "Still broken." in mail.outbox[0].body

    thread = client.get(f"/api/v1/support-cases/{case_id}/messages/").json()
    # Staff stay anonymous to customers.
    assert [(m["author_role"], m["author_name"]) for m in thread] == [
        ("support", "JT-Code support"),
        ("customer", "member@example.com"),
    ]


@pytest.mark.django_db
def test_a_closed_case_takes_no_replies(member, org, django_capture_on_commit_callbacks):
    case_id = _open(member, org, django_capture_on_commit_callbacks)["id"]
    client = _client(member, org)
    assert client.post(f"/api/v1/support-cases/{case_id}/close/").json()["status"] == "closed"
    reply = client.post(f"/api/v1/support-cases/{case_id}/messages/", {"body": "x"}, format="json")
    assert reply.status_code == 409


@pytest.mark.django_db
def test_customers_cannot_assign_resolve_or_edit_cases(member, org, django_capture_on_commit_callbacks):
    case_id = _open(member, org, django_capture_on_commit_callbacks)["id"]
    client = _client(member, org)
    other = User.objects.create(username="x", supabase_user_id="sb-x", email="x@example.com")
    # The old API let any editor assign a case to any user id in the system.
    assigned = client.post(f"/api/v1/support-cases/{case_id}/assign/", {"assigned_to_id": str(other.id)})
    assert assigned.status_code == 404
    assert client.post(f"/api/v1/support-cases/{case_id}/resolve/").status_code == 404
    assert (
        client.patch(f"/api/v1/support-cases/{case_id}/", {"status": "resolved"}, format="json").status_code
        == 405
    )
    assert client.delete(f"/api/v1/support-cases/{case_id}/").status_code == 405
    case = SupportCase.objects.get(id=case_id)
    assert case.status == SupportCase.Status.OPEN and case.assigned_to is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body", [{"subject": "x" * 201}, {"category": "nonsense"}, {"priority": "critical"}, {"description": ""}]
)
def test_invalid_cases_are_refused(member, org, body):
    payload = {"subject": "s", "description": "d", "category": "bug", **body}
    assert _client(member, org).post("/api/v1/support-cases/", payload, format="json").status_code == 400
    assert not SupportCase.objects.exists()


@pytest.mark.django_db
def test_opening_cases_is_rate_limited(member, org, settings, django_capture_on_commit_callbacks):
    settings.SUPPORT_CASES_PER_HOUR = 2
    _open(member, org, django_capture_on_commit_callbacks)
    _open(member, org, django_capture_on_commit_callbacks)
    third = _client(member, org).post(
        "/api/v1/support-cases/", {"subject": "s", "description": "d", "category": "bug"}, format="json"
    )
    assert third.status_code == 429
    assert SupportCase.objects.count() == 2


@pytest.mark.django_db
def test_a_staff_reply_typed_in_the_admin_is_sent_as_support(
    admin_client, admin_user, member, org, django_capture_on_commit_callbacks
):
    case = SupportCase.objects.create(
        organization=org, user=member, subject="Help", description="d", category="bug"
    )
    form = {
        "organization": str(org.id),
        "user": str(member.id),
        "status": "open",
        "priority": "medium",
        "category": "bug",
        "subject": "Help",
        "description": "d",
        "tags": "[]",
        "metadata": "{}",
        "messages-TOTAL_FORMS": "1",
        "messages-INITIAL_FORMS": "0",
        "messages-MIN_NUM_FORMS": "0",
        "messages-MAX_NUM_FORMS": "1000",
        "messages-0-body": "We are on it.",
    }
    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(f"/admin/governance/supportcase/{case.id}/change/", form)
    assert response.status_code == 302, response.content[:2000]
    (message,) = SupportCaseMessage.objects.filter(case=case)
    assert (message.author_role, message.author_id, message.body) == (
        "support",
        admin_user.id,
        "We are on it.",
    )
    case.refresh_from_db()
    assert case.status == SupportCase.Status.WAITING_USER
    assert mail.outbox[-1].to == ["member@example.com"]
