"""Least-privilege runtime database role and PgBouncer wiring (backlog Phase 5).

The role tests run the real provisioning SQL on the test database inside the test transaction (roles,
grants and policies roll back with it), then act as the role with ``SET LOCAL ROLE`` to prove what it can
and cannot do.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml
from django.core.management import CommandError, call_command
from django.db import DatabaseError, connection, transaction

from apps.core.management.commands.provision_app_role import POLICY, provision
from apps.identity.models import User

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "x" * 40


@pytest.fixture
def app_role(db):
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolcreaterole OR rolsuper FROM pg_roles WHERE rolname = current_user")
        if not cursor.fetchone()[0]:
            pytest.skip("the test database user cannot create roles")
        cursor.execute(
            "CREATE SCHEMA IF NOT EXISTS auth;"
            "CREATE TABLE IF NOT EXISTS auth.users (id uuid PRIMARY KEY, banned_until timestamptz,"
            " deleted_at timestamptz, encrypted_password text);"
            "CREATE TABLE IF NOT EXISTS auth.sessions"
            " (id uuid PRIMARY KEY, user_id uuid, not_after timestamptz)"
        )
        role = f"jt_app_test_{uuid.uuid4().hex[:10]}"
        summary = provision(cursor, role, PASSWORD)
    return role, summary


def _as_role(cursor, role: str) -> None:
    cursor.execute(f'SET LOCAL ROLE "{role}"')


def _denied(cursor, statement: str) -> bool:
    try:
        with transaction.atomic():
            cursor.execute(statement)
    except DatabaseError:
        return True
    return False


def test_role_has_no_dangerous_attributes(app_role):
    role, _ = app_role
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication, rolcanlogin "
            "FROM pg_roles WHERE rolname = %s",
            [role],
        )
        assert cursor.fetchone() == (False, False, False, False, False, True)


def test_role_reads_and_writes_application_tables_through_rls(app_role):
    role, _ = app_role
    User.objects.create(supabase_user_id="sb-role", email="role@example.com")
    with connection.cursor() as cursor:
        cursor.execute("SELECT relrowsecurity FROM pg_class WHERE oid = 'public.identity_user'::regclass")
        rls_on = cursor.fetchone()[0]
        _as_role(cursor, role)
        cursor.execute("SELECT count(*) FROM identity_user WHERE supabase_user_id = 'sb-role'")
        assert cursor.fetchone()[0] == 1
        cursor.execute("UPDATE identity_user SET bio = 'ok' WHERE supabase_user_id = 'sb-role'")
        assert cursor.rowcount == 1
        cursor.execute("RESET ROLE")
        if rls_on:
            cursor.execute(
                "SELECT 1 FROM pg_policies WHERE tablename = 'identity_user' AND policyname = %s", [POLICY]
            )
            assert cursor.fetchone()


def test_role_cannot_change_the_schema_or_read_auth(app_role):
    role, _ = app_role
    with connection.cursor() as cursor:
        _as_role(cursor, role)
        assert _denied(cursor, "CREATE TABLE public.jt_should_not_exist (id int)")
        assert _denied(cursor, "DROP TABLE identity_user")
        assert _denied(cursor, "SELECT encrypted_password FROM auth.users")
        assert _denied(cursor, "SELECT * FROM auth.sessions")
        cursor.execute("RESET ROLE")


def test_role_checks_sessions_only_through_the_definer_function(app_role):
    role, _ = app_role
    session, user = str(uuid.uuid4()), str(uuid.uuid4())
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO auth.users (id) VALUES (%s)", [user])
        cursor.execute("INSERT INTO auth.sessions (id, user_id) VALUES (%s, %s)", [session, user])
        _as_role(cursor, role)
        cursor.execute("SELECT public.jt_code_session_active(%s, %s)", [session, user])
        assert cursor.fetchone()[0] is True
        cursor.execute("SELECT public.jt_code_session_active(%s, %s)", [str(uuid.uuid4()), user])
        assert cursor.fetchone()[0] is False
        cursor.execute("RESET ROLE")


def test_role_manages_its_own_dashboard_tables(app_role):
    role, _ = app_role
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'viz_data'")
        if cursor.fetchone() is None:
            pytest.skip("viz_data schema not present")
        _as_role(cursor, role)
        cursor.execute("CREATE TABLE viz_data.jt_role_probe (id int)")
        cursor.execute("GRANT SELECT ON viz_data.jt_role_probe TO jt_viz_reader")
        cursor.execute("SET LOCAL ROLE jt_viz_reader")
        cursor.execute("SELECT count(*) FROM viz_data.jt_role_probe")
        cursor.execute(f'SET LOCAL ROLE "{role}"')
        cursor.execute("DROP TABLE viz_data.jt_role_probe")
        cursor.execute("RESET ROLE")


def test_provisioning_is_idempotent(app_role):
    role, first = app_role
    with connection.cursor() as cursor:
        again = provision(cursor, role, PASSWORD)
    assert again["policies"] == 0
    assert again["tables"] == first["tables"]


def test_command_refuses_a_weak_password(monkeypatch, db):
    monkeypatch.setenv("APP_DB_PASSWORD", "short")
    with pytest.raises(CommandError):
        call_command("provision_app_role")


# compose wiring ------------------------------------------------------------------------------


def _compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text())


def test_app_services_use_pgbouncer_as_the_runtime_role_and_migrate_stays_direct():
    compose = _compose()
    env = compose["x-app-env"]
    assert env["DATABASE_URL"].startswith("postgresql://jt_code_app:${APP_DB_PASSWORD")
    assert "@pgbouncer:5432/postgres" in env["DATABASE_URL"]
    assert env["DATABASE_POOLER_MODE"] == "transaction"
    assert env["DATABASE_CONN_MAX_AGE"] == "0"

    migrate = compose["services"]["migrate"]["environment"]
    assert migrate["DATABASE_URL"].startswith("postgresql://postgres:")
    assert "@supabase-db:5432/postgres" in migrate["DATABASE_URL"]
    assert migrate["DATABASE_POOLER_MODE"] == "direct"
    assert migrate["DATABASE_ADMIN_CONNECTION"] == "true"


def test_pgbouncer_pools_transactions_for_the_runtime_role_only():
    service = _compose()["services"]["pgbouncer"]
    env = service["environment"]
    assert service["image"].startswith("edoburu/pgbouncer:v1.")
    assert env["POOL_MODE"] == "transaction"
    assert env["AUTH_TYPE"] == "scram-sha-256"
    assert env["DATABASE_URL"].startswith("postgresql://jt_code_app:")
    assert "ports" not in service
    assert "healthcheck" in service


def test_migrate_provisions_the_role_when_a_password_is_set():
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text()
    migrate = entrypoint[entrypoint.index("  migrate)") :]
    assert migrate.index("manage.py migrate") < migrate.index("provision_app_role")


def test_validation_rejects_the_owner_role_for_the_app_on_a_single_host(monkeypatch):
    from config.settings import validation

    base = {
        "SINGLE_HOST_DEPLOYMENT": "true",
        "DATABASE_URL": "postgresql://postgres:pw@supabase-db:5432/postgres",
    }
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DATABASE_ADMIN_CONNECTION", raising=False)
    assert any("jt_code_app" in problem for problem in validation.database_role_problems())

    monkeypatch.setenv("DATABASE_ADMIN_CONNECTION", "true")
    assert validation.database_role_problems() == []
    monkeypatch.delenv("DATABASE_ADMIN_CONNECTION")
    monkeypatch.setenv("DATABASE_URL", "postgresql://jt_code_app:pw@pgbouncer:5432/postgres")
    assert validation.database_role_problems() == []
