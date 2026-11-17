"""Request-scoped memoization of tenancy checks and browser caching of catalogues (backlog Phase 6)."""

from __future__ import annotations

import time
import uuid
from collections import Counter

import jwt as pyjwt
import pytest
from django.core.cache import cache
from django.db import connection
from django.test.client import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request

from apps.identity.authorization import (
    organization_for_request,
    organization_ids_for_user,
    require_organization_membership,
    user_has_role,
)
from apps.identity.models import Organization, User, UserOrganization


@pytest.fixture
def member(db):
    user = User.objects.create(supabase_user_id=str(uuid.uuid4()), email="memo@example.com")
    organization = Organization.objects.create(name="Memo", owner=user)
    UserOrganization.objects.create(user=user, organization=organization)
    return user, organization


def _request(user, organization_id=None) -> Request:
    headers = {"HTTP_X_ORGANIZATION_ID": str(organization_id)} if organization_id else {}
    request = Request(RequestFactory().get("/", **headers))
    request.user = user
    return request


def test_memoized_checks_hit_the_database_once_per_request(member, django_assert_num_queries):
    user, organization = member
    user._jt_authz_cache = {}
    request = _request(user, organization.id)

    organization_for_request(request)
    user_has_role(user, "admin", organization.id)
    organization_ids_for_user(user)
    with django_assert_num_queries(0):
        assert organization_for_request(request) == organization
        assert user_has_role(user, "admin", organization.id) is True
        assert organization_ids_for_user(user) == [str(organization.id)]
        require_organization_membership(user, organization.id)


def test_users_without_the_request_memo_always_query(member, django_assert_num_queries):
    user, _ = member  # e.g. a Celery task's user: no _jt_authz_cache

    organization_ids_for_user(user)
    with django_assert_num_queries(1):
        organization_ids_for_user(user)


def test_memoized_membership_still_denies_other_and_malformed_organizations(member):
    user, _ = member
    other = Organization.objects.create(name="Other")
    user._jt_authz_cache = {}

    with pytest.raises(PermissionDenied):
        require_organization_membership(user, other.id)
    with pytest.raises(PermissionDenied):
        organization_for_request(_request(user, other.id))
    with pytest.raises(PermissionDenied):
        organization_for_request(_request(user, "not-a-uuid"))
    with pytest.raises(PermissionDenied):
        organization_for_request(_request(user, uuid.uuid4()))


@pytest.mark.django_db
def test_an_authenticated_request_repeats_no_tenancy_query(api_client, member):
    user, organization = member
    cache.clear()
    token = pyjwt.encode(
        {
            "sub": user.supabase_user_id,
            "email": user.email,
            "aud": "authenticated",
            "role": "authenticated",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        "test-jwt-secret",
        algorithm="HS256",
    )
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_ORGANIZATION_ID=str(organization.id))
    api_client.get(reverse("organization-members"))  # the first request provisions; measure the next one

    with CaptureQueriesContext(connection) as queries:
        response = api_client.get(reverse("organization-members"))

    assert response.status_code == 200
    tenancy = Counter(
        query["sql"]
        for query in queries.captured_queries
        if "identity_userorganization" in query["sql"] or "identity_userrole" in query["sql"]
    )
    assert tenancy and max(tenancy.values()) == 1, tenancy


# P6-2 -----------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_tenant_independent_catalogues_are_browser_cacheable(authenticated_client):
    plans = authenticated_client.get("/api/v1/plans/")
    catalog = authenticated_client.get(reverse("viz-catalog"))
    account = authenticated_client.get(reverse("account-me"))

    assert plans.status_code == 200 and plans["Cache-Control"] == "private, max-age=300"
    assert catalog.status_code == 200 and catalog["Cache-Control"] == "private, max-age=300"
    assert account["Cache-Control"] == "no-store"
