"""Profile integrity: Django profiles always reflect the Supabase sign-up data (backlog Phase 1)."""

from __future__ import annotations

import importlib
import json
import time
import uuid

import jwt as pyjwt
import pytest
from django.apps import apps as django_apps
from django.contrib.auth.hashers import make_password
from django.core.cache import cache
from django.core.management import call_command
from django.urls import reverse

from apps.governance.models import ConsentRecord
from apps.identity.models import User

SIGNUP_METADATA = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "contact": "0700 000000",
    "country": "UG",
    "countryName": "Uganda",
    "dialCode": "+256",
    "timezone": "Africa/Kampala",
}


def _token(sub: str, email: str, metadata: dict | None = None) -> str:
    claims = {
        "sub": sub,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "session_id": str(uuid.uuid4()),
    }
    if metadata is not None:
        claims["user_metadata"] = metadata
    return pyjwt.encode(claims, "test-jwt-secret", algorithm="HS256")


def _ping(client, token: str):
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return client.get(reverse("auth-ping"))


@pytest.fixture(autouse=True)
def _clear_provisioning_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_new_user_gets_profile_unusable_password_and_signup_consents(api_client):
    metadata = {**SIGNUP_METADATA, "terms_accepted": True, "privacy_accepted": True, "consent_version": "1.0"}

    assert _ping(api_client, _token("sb-new", "ada@example.com", metadata)).status_code == 200

    user = User.objects.get(supabase_user_id="sb-new")
    assert (user.first_name, user.last_name, user.full_name) == ("Ada", "Lovelace", "Ada Lovelace")
    assert (user.contact, user.country, user.country_code, user.dial_code) == (
        "0700 000000",
        "Uganda",
        "UG",
        "+256",
    )
    assert user.password.startswith("!") and not user.has_usable_password()
    consents = set(
        ConsentRecord.objects.filter(user=user, status=ConsentRecord.Status.GRANTED).values_list(
            "consent_type", flat=True
        )
    )
    assert consents == {ConsentRecord.ConsentType.TERMS, ConsentRecord.ConsentType.PRIVACY}
    account = api_client.get(reverse("account-me")).json()
    assert account["termsAccepted"] is True and account["privacyAccepted"] is True
    assert (account["firstName"], account["lastName"]) == ("Ada", "Lovelace")


@pytest.mark.django_db
def test_existing_email_only_row_is_completed_on_the_next_sign_in(api_client):
    """The reported bug: a row created with only an e-mail was never filled afterwards."""
    User.objects.create(supabase_user_id="sb-old", email="old@example.com", password="")

    assert _ping(api_client, _token("sb-old", "old@example.com", SIGNUP_METADATA)).status_code == 200

    user = User.objects.get(supabase_user_id="sb-old")
    assert user.full_name == "Ada Lovelace"
    assert user.timezone == "Africa/Kampala"


@pytest.mark.django_db
def test_profile_edits_are_never_overwritten_by_session_metadata(api_client):
    User.objects.create(
        supabase_user_id="sb-edited", email="e@example.com", first_name="Grace", full_name="Grace Hopper"
    )

    _ping(api_client, _token("sb-edited", "e@example.com", SIGNUP_METADATA))

    user = User.objects.get(supabase_user_id="sb-edited")
    assert (user.first_name, user.full_name) == ("Grace", "Grace Hopper")
    assert user.last_name == "Lovelace"  # empty before, so filled


@pytest.mark.django_db
def test_withdrawn_consent_is_not_regranted_by_the_signup_flag(api_client):
    metadata = {**SIGNUP_METADATA, "terms_accepted": True}
    _ping(api_client, _token("sb-consent", "c@example.com", metadata))
    user = User.objects.get(supabase_user_id="sb-consent")
    ConsentRecord.objects.filter(user=user).update(status=ConsentRecord.Status.WITHDRAWN)
    cache.clear()

    _ping(api_client, _token("sb-consent", "c@example.com", metadata))

    assert list(ConsentRecord.objects.filter(user=user).values_list("status", flat=True)) == ["withdrawn"]


def test_oauth_profile_metadata_is_split_into_names():
    from apps.identity.provisioning import profile_values_from_metadata

    values = profile_values_from_metadata(
        {"full_name": "Grace Brewster Hopper", "avatar_url": "https://example.com/a.png", "country": "1x"}
    )

    assert values["first_name"] == "Grace"
    assert values["last_name"] == "Brewster Hopper"
    assert values["avatar_url"] == "https://example.com/a.png"
    assert "country_code" not in values
    assert profile_values_from_metadata({"avatar_url": "javascript:alert(1)"}) == {}


# Supabase Database Webhook ---------------------------------------------------------------------


@pytest.fixture
def webhook_secret(settings):
    settings.SUPABASE_WEBHOOK_SIGNING_SECRET = "supabase-db-webhook-secret"
    return settings.SUPABASE_WEBHOOK_SIGNING_SECRET


def _webhook(client, secret, event_type, record):
    return client.post(
        reverse("supabase-webhook"),
        data=json.dumps({"type": event_type, "table": "users", "schema": "auth", "record": record}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {secret}",
    )


@pytest.mark.django_db
def test_database_webhook_reads_raw_user_meta_data(client, webhook_secret):
    record = {"id": "sb-hook", "email": "hook@example.com", "raw_user_meta_data": SIGNUP_METADATA}

    assert _webhook(client, webhook_secret, "INSERT", record).status_code == 200

    user = User.objects.get(supabase_user_id="sb-hook")
    assert user.full_name == "Ada Lovelace"
    assert user.display_name == "Ada Lovelace"
    assert not user.has_usable_password()


@pytest.mark.django_db
def test_routine_auth_users_update_never_blanks_the_profile(client, webhook_secret):
    User.objects.create(
        supabase_user_id="sb-keep", email="keep@example.com", full_name="Kept Name", display_name="Kept"
    )
    record = {
        "id": "sb-keep",
        "email": "keep@example.com",
        "raw_user_meta_data": {},
        "last_sign_in_at": "now",
    }

    assert _webhook(client, webhook_secret, "UPDATE", record).status_code == 200

    user = User.objects.get(supabase_user_id="sb-keep")
    assert (user.full_name, user.display_name) == ("Kept Name", "Kept")


@pytest.mark.django_db
def test_webhook_update_does_not_reactivate_a_suspended_user(client, webhook_secret):
    User.objects.create(supabase_user_id="sb-off", email="off@example.com", is_active=False)

    _webhook(client, webhook_secret, "UPDATE", {"id": "sb-off", "email": "off@example.com"})

    assert User.objects.get(supabase_user_id="sb-off").is_active is False


# Account API -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_account_patch_keeps_all_name_fields_consistent(authenticated_client, user):
    user.full_name, user.first_name, user.last_name = "Ada Integration", "Ada", "Integration"
    user.save()

    response = authenticated_client.patch(reverse("account-me"), {"lastName": "Lovelace"}, format="json")

    assert response.status_code == 200, response.content
    user.refresh_from_db()
    assert (user.first_name, user.last_name, user.full_name) == ("Ada", "Lovelace", "Ada Lovelace")
    assert (response.json()["firstName"], response.json()["lastName"]) == ("Ada", "Lovelace")


# Commands --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_sync_supabase_profiles_fills_empty_rows_and_dry_run_writes_nothing(monkeypatch):
    User.objects.create(supabase_user_id="sb-sync", email="sync@example.com")
    command = importlib.import_module("apps.identity.management.commands.sync_supabase_profiles")
    monkeypatch.setattr(
        command,
        "iter_auth_users",
        lambda: iter([{"id": "sb-sync", "email": "sync@example.com", "user_metadata": SIGNUP_METADATA}]),
    )

    call_command("sync_supabase_profiles", "--dry-run")
    assert User.objects.get(supabase_user_id="sb-sync").full_name == ""

    call_command("sync_supabase_profiles")
    assert User.objects.get(supabase_user_id="sb-sync").full_name == "Ada Lovelace"


@pytest.mark.django_db
def test_create_local_account_sends_and_stores_the_name(monkeypatch, settings):
    settings.DEBUG = True
    command = importlib.import_module("apps.identity.management.commands.create_local_account")
    sent = {}

    def fake_create(email, password, metadata=None):
        sent.update(email=email, metadata=metadata)
        return "sb-local", True

    monkeypatch.setattr(command, "create_local_login", fake_create)
    monkeypatch.setattr(command.getpass, "getpass", lambda _prompt: "a-long-password-1")

    call_command(
        "create_local_account", "local@example.com", "--first-name", "Ada", "--last-name", "Lovelace"
    )

    assert sent["metadata"] == {"first_name": "Ada", "last_name": "Lovelace"}
    user = User.objects.get(supabase_user_id="sb-local")
    assert user.full_name == "Ada Lovelace"
    assert not user.has_usable_password()


@pytest.mark.django_db
def test_migration_marks_empty_passwords_unusable():
    empty = User.objects.create(supabase_user_id="sb-empty", email="x@example.com", password="")
    kept = User.objects.create(
        supabase_user_id="sb-kept", email="y@example.com", password=make_password("pw")
    )
    migration = importlib.import_module("apps.identity.migrations.0009_unusable_passwords")

    migration.mark_unusable(django_apps, None)

    empty.refresh_from_db()
    kept.refresh_from_db()
    assert empty.password.startswith("!")
    assert kept.check_password("pw")


@pytest.mark.django_db
def test_concurrent_first_requests_record_each_consent_once(monkeypatch, settings):
    # A new user's first page load sends several requests at once. Between one request's existence
    # check and its insert, another has already recorded the consent: that answered 500
    # (IntegrityError on uniq_consent_user_type_version) instead of being treated as done.
    from apps.identity.models import Organization
    from apps.identity.provisioning import record_signup_consents

    user = User.objects.create(username="race", supabase_user_id="sb-race", email="race@example.com")
    organization = Organization.objects.create(name="Race workspace", owner=user)
    ConsentRecord.objects.create(
        user=user,
        organization=organization,
        consent_type=ConsentRecord.ConsentType.TERMS,
        status=ConsentRecord.Status.GRANTED,
        version=settings.CONSENT_VERSION,
    )
    # The race window: this request's existence check did not see the concurrent insert.
    monkeypatch.setattr(ConsentRecord.objects, "filter", lambda *args, **kwargs: ConsentRecord.objects.none())

    claims = {"user_metadata": {"terms_accepted": True, "privacy_accepted": True}}
    assert record_signup_consents(user, organization, claims) == 1  # privacy; terms already recorded

    monkeypatch.undo()
    assert sorted(ConsentRecord.objects.filter(user=user).values_list("consent_type", flat=True)) == sorted(
        [ConsentRecord.ConsentType.TERMS, ConsentRecord.ConsentType.PRIVACY]
    )


@pytest.fixture
def stub_auth_schema():
    """GoTrue's auth.users/auth.identities columns the local fallback writes (rolled back with the test)."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("CREATE SCHEMA IF NOT EXISTS extensions")
        cursor.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto SCHEMA extensions")
        cursor.execute("CREATE SCHEMA IF NOT EXISTS auth")
        cursor.execute("DROP TABLE IF EXISTS auth.identities, auth.users")
        cursor.execute(
            "CREATE TABLE auth.users (instance_id uuid, id uuid PRIMARY KEY, aud text, role text, email text,"
            " encrypted_password text, email_confirmed_at timestamptz, raw_app_meta_data jsonb,"
            " raw_user_meta_data jsonb, created_at timestamptz, updated_at timestamptz,"
            " confirmation_token text, recovery_token text, email_change_token_new text, email_change text)"
        )
        cursor.execute(
            "CREATE TABLE auth.identities (provider_id text, user_id uuid, identity_data jsonb,"
            " provider text, created_at timestamptz, updated_at timestamptz)"
        )
    return connection


@pytest.mark.django_db
def test_local_login_falls_back_to_the_auth_schema_when_the_admin_api_is_closed(stub_auth_schema, settings):
    # Since Phase 3 /auth/v1/admin is internal-only, so manage.py on the host cannot create logins.
    from apps.identity.local_auth import create_local_login

    settings.DEBUG = True
    settings.SUPABASE_SECRET_KEY = ""  # the host has no admin key: the Admin API path refuses at once

    user_id, created = create_local_login("Viewer@Example.com", "a-long-password-1", {"first_name": "Ada"})
    assert created
    with stub_auth_schema.cursor() as cursor:
        cursor.execute(
            "SELECT email, email_confirmed_at IS NOT NULL, encrypted_password = extensions.crypt(%s,"
            " encrypted_password), raw_user_meta_data->>'first_name' FROM auth.users WHERE id = %s",
            ["a-long-password-1", user_id],
        )
        assert cursor.fetchone() == ("viewer@example.com", True, True, "Ada")
        cursor.execute("SELECT provider FROM auth.identities WHERE user_id = %s", [user_id])
        assert cursor.fetchone() == ("email",)

    # The same address again reuses the login (as the Admin API path does).
    assert create_local_login("viewer@example.com", "another-password-1") == (user_id, False)


@pytest.mark.django_db
def test_local_login_never_writes_the_auth_schema_outside_debug(stub_auth_schema, settings):
    from apps.identity.local_auth import create_local_login
    from apps.identity.supabase_admin import SupabaseAdminError

    settings.DEBUG = False
    settings.SUPABASE_SECRET_KEY = ""
    with pytest.raises(SupabaseAdminError):
        create_local_login("prod@example.com", "a-long-password-1")
    with stub_auth_schema.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM auth.users")
        assert cursor.fetchone() == (0,)
