"""Django tables created after the Data API lockdown migration still get Row Level Security."""

from __future__ import annotations

import pytest
from django.db import connection

from apps.governance.data_api import enable_row_level_security

pytestmark = pytest.mark.django_db


def _has_supabase_roles() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_roles WHERE rolname = 'anon'")
        return bool(cursor.fetchone()[0])


def _rls_enabled(table: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rowsecurity FROM pg_tables WHERE schemaname = 'public' AND tablename = %s", [table]
        )
        return cursor.fetchone()[0]


def test_post_migrate_enables_rls_on_tables_created_after_the_lockdown():
    if not _has_supabase_roles():
        pytest.skip("requires a Supabase PostgreSQL server with the Data API roles")
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE public.jt_rls_probe (id bigint PRIMARY KEY)")
    assert _rls_enabled("jt_rls_probe") is False

    enable_row_level_security(using="default")

    assert _rls_enabled("jt_rls_probe") is True


def test_every_migrated_public_table_has_rls():
    if not _has_supabase_roles():
        pytest.skip("requires a Supabase PostgreSQL server with the Data API roles")
    with connection.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND NOT rowsecurity")
        assert cursor.fetchall() == []
