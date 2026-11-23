"""NUL characters are refused at the edge (400), never reach PostgreSQL (which cannot store them: 500)."""

from __future__ import annotations

import pytest

from apps.conversations.models import Conversation
from apps.identity.models import Organization


@pytest.fixture
def org(user):
    organization = Organization.objects.create(name="NUL guard", owner=user)
    user.organizations.add(organization)
    return organization


def _refused(response) -> None:
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "invalid"
    # Answered after SecurityHeadersMiddleware, so the refusal carries the usual headers.
    assert "Content-Security-Policy" in response and response["X-Content-Type-Options"] == "nosniff"


@pytest.mark.django_db
def test_nul_in_a_query_string_is_refused(authenticated_client, org):
    _refused(authenticated_client.get("/api/v1/conversations/?q=abc%00def"))
    _refused(authenticated_client.get("/api/v1/conversations/?q=abc%00"))
    assert authenticated_client.get("/api/v1/conversations/?q=abc").status_code == 200


@pytest.mark.django_db
def test_a_json_nul_escape_is_refused_but_escaped_backslashes_are_text(authenticated_client, org):
    refused = authenticated_client.generic(
        "POST", "/api/v1/conversations/", '{"title": "bad\\u0000title"}', content_type="application/json"
    )
    _refused(refused)
    assert not Conversation.objects.filter(organization=org).exists()

    # A title that *shows* the six characters \u0000 (the backslash itself escaped) is ordinary text.
    accepted = authenticated_client.generic(
        "POST",
        "/api/v1/conversations/",
        '{"title": "code \\\\u0000 sample"}',
        content_type="application/json",
    )
    assert accepted.status_code == 201, accepted.content
    assert Conversation.objects.get(organization=org).title == "code \\u0000 sample"
