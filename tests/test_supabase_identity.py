"""Supabase is the identity source of truth: sessions, user sync webhook and account deletion."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import httpx
import jwt as pyjwt
import pytest
from django.urls import reverse
from django.utils import timezone

import apps.identity.authentication as auth_module
from apps.identity.models import User


def _token(**claims):
    payload = {
        "sub": "test-supabase-user-id",
        "email": "test@example.com",
        "aud": "authenticated",
        "role": "authenticated",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "jti": str(uuid.uuid4()),
        **claims,
    }
    return pyjwt.encode(payload, "test-jwt-secret", algorithm="HS256")


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["anon", "service_role", None])
def test_only_authenticated_user_sessions_are_accepted(api_client, role):
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {_token(role=role)}")

    assert api_client.get(reverse("auth-ping")).status_code == 401


@pytest.mark.django_db
def test_anonymous_supabase_sessions_are_rejected_by_default(api_client, settings):
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {_token(is_anonymous=True)}")
    assert api_client.get(reverse("auth-ping")).status_code == 401

    settings.SUPABASE_ALLOW_ANONYMOUS_USERS = True
    assert api_client.get(reverse("auth-ping")).status_code == 200


@pytest.mark.django_db
def test_email_changes_in_supabase_are_reflected_locally(api_client, user):
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {_token(email='renamed@example.com')}")

    assert api_client.get(reverse("auth-ping")).status_code == 200
    user.refresh_from_db()
    assert user.email == "renamed@example.com"


def test_unknown_kid_refresh_is_rate_limited(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url):
            calls.append(url)
            return httpx.Response(200, json={"keys": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(auth_module.httpx, "Client", FakeClient)
    monkeypatch.setattr(auth_module, "_JWKS_CACHE", None)
    monkeypatch.setattr(auth_module, "_JWKS_FETCHED_AT", 0.0)

    auth_module._fetch_jwks()
    for _ in range(5):
        auth_module._fetch_jwks(force_refresh=True)

    assert len(calls) == 1


def _post_webhook(client, payload, **headers):
    return client.post(
        reverse("supabase-webhook"), data=json.dumps(payload), content_type="application/json", **headers
    )


@pytest.fixture
def webhook_secret(settings):
    settings.SUPABASE_WEBHOOK_SIGNING_SECRET = "supabase-db-webhook-secret"
    return settings.SUPABASE_WEBHOOK_SIGNING_SECRET


@pytest.mark.django_db
def test_supabase_database_webhook_bearer_secret_syncs_user(client, webhook_secret):
    payload = {
        "type": "INSERT",
        "record": {"id": "sb-user-1", "email": "new@example.com", "user_metadata": {"full_name": "New"}},
    }

    rejected = _post_webhook(client, payload, HTTP_AUTHORIZATION="Bearer wrong")
    accepted = _post_webhook(client, payload, HTTP_AUTHORIZATION=f"Bearer {webhook_secret}")

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert User.objects.get(supabase_user_id="sb-user-1").full_name == "New"


@pytest.mark.django_db
def test_supabase_webhook_signed_request_is_accepted_once(client, webhook_secret):
    from apps.core.signing import sign_request

    body = json.dumps({"type": "INSERT", "record": {"id": "sb-user-2", "email": "h@example.com"}})
    headers = {
        f"HTTP_{k.upper().replace('-', '_')}": v
        for k, v in sign_request(body.encode(), webhook_secret).items()
    }

    response = client.post(reverse("supabase-webhook"), data=body, content_type="application/json", **headers)
    replay = client.post(reverse("supabase-webhook"), data=body, content_type="application/json", **headers)
    legacy = client.post(
        reverse("supabase-webhook"),
        data=body,
        content_type="application/json",
        HTTP_X_SUPABASE_SIGNATURE=hmac.new(
            webhook_secret.encode(), body.encode(), hashlib.sha256
        ).hexdigest(),
    )

    assert response.status_code == 200
    assert User.objects.filter(supabase_user_id="sb-user-2").exists()
    assert replay.status_code == 401  # the nonce was already consumed
    assert legacy.status_code == 401  # untimed HMACs are replayable and no longer accepted


@pytest.mark.django_db
@pytest.mark.parametrize(
    "record_extra",
    [
        {"banned_until": (timezone.now() + timezone.timedelta(days=1)).isoformat()},
        {"deleted_at": timezone.now().isoformat()},
    ],
)
def test_banned_or_deleted_supabase_users_are_deactivated(client, user, webhook_secret, record_extra):
    payload = {"type": "UPDATE", "record": {"id": user.supabase_user_id, "email": user.email, **record_extra}}

    response = _post_webhook(client, payload, HTTP_AUTHORIZATION=f"Bearer {webhook_secret}")

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.is_active is False


@pytest.mark.django_db
def test_account_deletion_removes_the_supabase_auth_user_first(authenticated_client, user, monkeypatch):
    deleted = []
    monkeypatch.setattr(
        "apps.identity.supabase_admin.delete_auth_user", lambda supabase_id: deleted.append(supabase_id)
    )

    response = authenticated_client.delete(reverse("settings-account"))

    assert response.status_code == 200
    assert deleted == [user.supabase_user_id]
    user.refresh_from_db()
    assert user.is_active is False and user.email == ""


@pytest.mark.django_db
def test_account_is_kept_when_supabase_deletion_fails(authenticated_client, user, monkeypatch):
    from apps.identity.supabase_admin import SupabaseAdminError

    def refuse(_supabase_id):
        raise SupabaseAdminError("unavailable")

    monkeypatch.setattr("apps.identity.supabase_admin.delete_auth_user", refuse)

    response = authenticated_client.delete(reverse("settings-account"))

    assert response.status_code == 502
    user.refresh_from_db()
    assert user.is_active is True and user.email == "test@example.com"


def test_admin_client_uses_the_server_secret_key(monkeypatch, settings):
    from apps.identity import supabase_admin

    settings.SUPABASE_URL = "https://project.supabase.co"
    seen = {}

    def fake_delete(url, headers, timeout):
        seen.update(url=url, headers=headers)
        return httpx.Response(204, request=httpx.Request("DELETE", url))

    monkeypatch.setattr(supabase_admin.httpx, "delete", fake_delete)

    supabase_admin.delete_auth_user("abc")

    assert seen["url"] == "https://project.supabase.co/auth/v1/admin/users/abc"
    assert seen["headers"]["apikey"] == settings.SUPABASE_SECRET_KEY
