"""Keep Row Level Security on every Django-owned table, including ones created after the lockdown.

Migration ``governance.0011`` revokes the Supabase Data API roles' privileges and
enables RLS on the tables that existed when it ran. Revocation covers future
tables through ``ALTER DEFAULT PRIVILEGES``, but RLS has no default, so tables
created by later migrations would be left without it. This runs after every
``migrate`` and closes that gap. It is a no-op without the Supabase roles.
"""

from __future__ import annotations

from typing import Any

from django.db import connections

ENABLE_RLS_SQL = """
DO $$
DECLARE
    tbl record;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        FOR tbl IN
            SELECT tablename FROM pg_tables
            WHERE schemaname = 'public' AND tableowner = current_user AND NOT rowsecurity
        LOOP
            EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl.tablename);
        END LOOP;
    END IF;
END
$$;
"""


def enable_row_level_security(sender: Any = None, **kwargs: Any) -> None:
    connection = connections[kwargs.get("using", "default")]
    if connection.vendor != "postgresql":
        return
    # A raw cursor without params: psycopg would treat format()'s %I as placeholders.
    with connection.cursor() as cursor:
        cursor.execute(ENABLE_RLS_SQL)
