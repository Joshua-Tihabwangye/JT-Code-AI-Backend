"""API authentication hardening (backlog Phase 2): revocation, strict claims, lockout, invites, admin."""

from __future__ import annotations

import importlib
import time
import uuid
from pathlib import Path

import jwt as pyjwt
import pytest
from django.apps import apps as django_apps
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection
from django.urls import clear_url_caches, reverse

import apps.identity.sessions as sessions_module
from apps.identity.models import Organization, OrganizationInvite, User, UserOrganization, hash_invite_token

ROOT = Path(__file__).resolve().parents[1]
USER_ID = str(uuid.uuid4())


def _token(**overrides) -> str:
    claims = {
        "sub": USER_ID,
        "email": "hardening@example.com",
        "aud": "authenticated",
        "role": "authenticated",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "session_id": str(uuid.uuid4()),
        **overrides,
    }
    for key in [key for key, value in claims.items() if value is None]:
        claims.pop(key)
    return pyjwt.encode(claims, "test-jwt-secret", algorithm="HS256")


def _ping(client, token: str):
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client.get(reverse("auth-ping"))


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


# P2-1 session revocation ------------------------------------------------------------------------


@pytest.fixture
def auth_schema():
    """A minimal GoTrue ``auth`` schema inside the test transaction (rolled back afterwards)."""
    with connection.cursor() as cursor:
        cursor.execute("CREATE SCHEMA IF NOT EXISTS auth")
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS auth.users ("
            " id uuid PRIMARY KEY, banned_until timestamptz, deleted_at timestamptz)"
        )
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS auth.sessions ("
            " id uuid PRIMARY KEY, user_id uuid NOT NULL, not_after timestamptz)"
        )

    def add(session_id: str, *, banned=False, deleted=False, expired=False) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO auth.users (id, banned_until, deleted_at) VALUES (%s, %s, %s)"
                " ON CONFLICT (id) DO UPDATE SET banned_until = EXCLUDED.banned_until,"
                " deleted_at = EXCLUDED.deleted_at",
                [
                    USER_ID,
                    "2999-01-01" if banned else None,
                    "2000-01-01" if deleted else None,
                ],
            )
            cursor.execute(
                "INSERT INTO auth.sessions (id, user_id, not_after) VALUES (%s, %s, %s)",
                [session_id, USER_ID, "2000-01-01" if expired else None],
            )

    return add


@pytest.mark.django_db
def test_live_session_is_accepted_and_cached(api_client, settings, auth_schema, monkeypatch):
    settings.SUPABASE_SESSION_CHECK = True
    session_id = str(uuid.uuid4())
    auth_schema(session_id)
    token = _token(session_id=session_id)

    assert _ping(api_client, token).status_code == 200

    def no_db(*_args):
        raise AssertionError("a cached session must not hit the database")

    monkeypatch.setattr(sessions_module, "session_is_active", no_db)
    assert _ping(api_client, token).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state",
    [{}, {"banned": True}, {"deleted": True}, {"expired": True}],
    ids=["signed-out", "banned", "deleted", "expired"],
)
def test_ended_sessions_are_rejected(api_client, settings, auth_schema, state):
    settings.SUPABASE_SESSION_CHECK = True
    session_id = str(uuid.uuid4())
    if state:
        auth_schema(session_id, **state)
    else:
        auth_schema(str(uuid.uuid4()))  # the user has another session, not this one (signed out)

    response = _ping(api_client, _token(session_id=session_id))

    assert response.status_code == 401


@pytest.mark.django_db
def test_tokens_without_a_session_are_rejected_when_the_check_is_on(api_client, settings):
    settings.SUPABASE_SESSION_CHECK = True

    assert _ping(api_client, _token(session_id=None)).status_code == 401


@pytest.mark.django_db
def test_session_check_fails_closed_on_database_errors(api_client, settings, monkeypatch):
    from django.db import DatabaseError

    settings.SUPABASE_SESSION_CHECK = True

    def broken(*_args):
        raise DatabaseError("auth schema unavailable")

    monkeypatch.setattr(sessions_module, "session_is_active", broken)

    assert _ping(api_client, _token()).status_code == 401


@pytest.mark.django_db
def test_session_check_is_off_by_default(api_client, settings):
    assert settings.SUPABASE_SESSION_CHECK is False
    assert _ping(api_client, _token(session_id=None)).status_code == 200


def test_both_compose_stacks_turn_the_session_check_on():
    assert 'SUPABASE_SESSION_CHECK: "true"' in (ROOT / "compose.yaml").read_text()


# P2-2 strict claims -----------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "overrides",
    [{"exp": None}, {"iat": None}, {"aud": None}, {"aud": "other"}, {"iat": int(time.time()) + 3600}],
    ids=["no-exp", "no-iat", "no-aud", "wrong-aud", "future-iat"],
)
def test_tokens_missing_or_with_bad_claims_are_rejected(api_client, overrides):
    assert _ping(api_client, _token(**overrides)).status_code == 401


@pytest.mark.django_db
def test_oversized_and_non_ascii_tokens_are_rejected(api_client):
    oversized = _token(padding="x" * 9000)
    assert _ping(api_client, oversized).status_code == 401
    api_client.credentials(HTTP_AUTHORIZATION="Bearer töken")
    assert api_client.get(reverse("auth-ping")).status_code == 401


@pytest.mark.django_db
def test_hs256_tokens_signed_with_another_algorithm_are_rejected(api_client):
    forged = pyjwt.encode(
        {"sub": USER_ID, "aud": "authenticated", "role": "authenticated", "iat": 1, "exp": 2**31},
        "test-jwt-secret",
        algorithm="HS512",
    )
    assert _ping(api_client, forged).status_code == 401


# P2-3 failed-auth lockout -----------------------------------------------------------------------


@pytest.mark.django_db
def test_an_ip_sending_invalid_tokens_is_locked_out(api_client, settings):
    settings.AUTH_FAILURE_LIMIT = 3

    statuses = [_ping(api_client, "not-a-jwt").status_code for _ in range(5)]
    locked = _ping(api_client, _token())

    assert statuses[:3] == [401, 401, 401]
    assert 429 in statuses[3:]
    assert locked.status_code == 429


@pytest.mark.django_db
def test_a_few_invalid_tokens_do_not_lock_out(api_client, settings):
    settings.AUTH_FAILURE_LIMIT = 3

    for _ in range(2):
        _ping(api_client, "not-a-jwt")

    assert _ping(api_client, _token()).status_code == 200


# P2-4 trusted proxies ---------------------------------------------------------------------------


def test_uvicorn_never_trusts_forwarded_headers_from_everyone():
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text()
    assert "FORWARDED_ALLOW_IPS:-*" not in entrypoint
    assert "FORWARDED_ALLOW_IPS:-127.0.0.1" in entrypoint


# P2-5 invite tokens hashed at rest --------------------------------------------------------------


@pytest.mark.django_db
def test_invites_store_only_a_digest_and_accept_the_raw_token(authenticated_client, user):
    from rest_framework.test import APIClient

    organization = Organization.objects.create(name="Invites", owner=user)
    UserOrganization.objects.create(user=user, organization=organization)
    created = authenticated_client.post(
        reverse("organization-members"),
        {"email": "guest@example.com", "role": "editor"},
        format="json",
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )
    assert created.status_code == 201, created.content
    raw = created.json()["token"]
    invite = OrganizationInvite.objects.get(id=created.json()["id"])
    assert invite.token == hash_invite_token(raw) and raw not in invite.token

    guest = User.objects.create(supabase_user_id="sb-guest", email="guest@example.com")
    client = APIClient()
    client.force_authenticate(guest)
    url = reverse("organization-invite-accept", args=[invite.id])
    assert (
        client.post(url, {"token": invite.token}, format="json").status_code == 403
    )  # the digest is useless
    assert client.post(url, {"token": raw}, format="json").status_code == 200


@pytest.mark.django_db
def test_migration_hashes_existing_raw_invite_tokens(user):
    organization = Organization.objects.create(name="Old invites", owner=user)
    invite = OrganizationInvite.objects.create(
        organization=organization, email="old@example.com", role="viewer", token="raw-legacy-token"
    )
    migration = importlib.import_module("apps.identity.migrations.0010_hash_invite_tokens")

    migration.hash_tokens(django_apps, None)
    migration.hash_tokens(django_apps, None)  # idempotent: a digest is never hashed again

    invite.refresh_from_db()
    assert invite.token == hash_invite_token("raw-legacy-token")


# P2-6 admin -------------------------------------------------------------------------------------


def test_admin_passwords_need_twelve_characters_and_must_not_be_common():
    for weak in ("short1", "password1234", "123456789012"):
        with pytest.raises(ValidationError):
            validate_password(weak)
    validate_password("correct-horse-battery-staple")


def test_admin_url_is_absent_when_disabled(settings):
    from django.urls import Resolver404, resolve

    import config.urls

    settings.DJANGO_ADMIN_ENABLED = False
    try:
        clear_url_caches()
        importlib.reload(config.urls)
        with pytest.raises(Resolver404):
            resolve("/admin/login/")
    finally:
        settings.DJANGO_ADMIN_ENABLED = True
        importlib.reload(config.urls)
        clear_url_caches()
    assert resolve("/admin/login/").url_name == "login"
