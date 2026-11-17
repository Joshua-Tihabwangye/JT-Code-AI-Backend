"""Create or update the least-privilege runtime database role (backlog P5-2).

    APP_DB_PASSWORD=... python manage.py provision_app_role

Runs as the migration role (the database owner, ``postgres`` in compose) inside the ``migrate`` container
after ``migrate``, on every deploy, so grants and policies keep up with new tables. The API, workers, beat
and consumer then connect as ``APP_DB_ROLE`` (default ``jt_code_app``) through PgBouncer.

The runtime role can only do what the application does:

* DML on every ``public`` table, its sequences, and future tables (default privileges); no DDL there;
* through row level security: Django tables keep RLS on (governance.0011) as a wall against the Supabase
  Data API, and the role gets one permissive policy per RLS table instead of ``BYPASSRLS``;
* the dashboard store's own schema ``viz_data``: create, own and drop its tables, and act as
  ``jt_viz_reader`` for dashboard queries;
* execute ``public.jt_code_session_active`` (the session-revocation check), with no access to the
  ``auth`` schema itself: no password hashes, refresh tokens or other GoTrue data;
* no superuser, no CREATEROLE, no CREATEDB, no BYPASSRLS, no ``storage`` schema.

Idempotent; the password is (re)set from ``APP_DB_PASSWORD`` on every run and is never printed.
"""

from __future__ import annotations

import os
import re

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection, transaction
from psycopg import sql

POLICY = "jt_code_app_access"
SESSION_FUNCTION = "public.jt_code_session_active(uuid, uuid)"
_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _exec(cursor, statement: sql.Composable | str, params: list | None = None) -> None:
    text = statement.as_string(cursor.connection) if isinstance(statement, sql.Composable) else statement
    cursor.execute(text, params)


def _exists(cursor, query: str, params: list) -> bool:
    cursor.execute(query, params)
    return cursor.fetchone() is not None


class Command(BaseCommand):
    help = "Create or update the least-privilege runtime database role (APP_DB_ROLE / APP_DB_PASSWORD)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--role", default=os.environ.get("APP_DB_ROLE", "jt_code_app"))

    def handle(self, *args, **options) -> None:
        role = options["role"]
        password = os.environ.get("APP_DB_PASSWORD", "")
        if not _ROLE_NAME.match(role):
            raise CommandError("The role name must be a lowercase PostgreSQL identifier.")
        if len(password) < 32:
            raise CommandError("APP_DB_PASSWORD must be set to a random value of at least 32 characters.")
        if connection.vendor != "postgresql":
            raise CommandError("provision_app_role needs PostgreSQL.")
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                summary = provision(cursor, role, password)
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Runtime role {role}: {summary['tables']} tables, {summary['policies']} new RLS policies, "
                f"{summary['viz_tables']} viz_data tables owned."
            )
        )


def provision(cursor, role: str, password: str) -> dict[str, int]:
    """Apply every grant for ``role`` on the current connection (one transaction). Returns counts."""
    ident = sql.Identifier(role)
    cursor.execute("SELECT current_user, current_database()")
    owner, database = cursor.fetchone()
    owner_ident = sql.Identifier(owner)

    # A new role starts with every dangerous attribute off (NOSUPERUSER, NOCREATEDB, NOCREATEROLE,
    # NOBYPASSRLS, NOREPLICATION). A non-superuser owner may not even mention those attributes on
    # PostgreSQL 16+, so only LOGIN and the password are set, and the result is verified below.
    if not _exists(cursor, "SELECT 1 FROM pg_roles WHERE rolname = %s", [role]):
        _exec(cursor, sql.SQL("CREATE ROLE {} LOGIN").format(ident))
    _exec(cursor, sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(ident, sql.Literal(password)))
    cursor.execute(
        "SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls OR rolreplication "
        "FROM pg_roles WHERE rolname = %s",
        [role],
    )
    if cursor.fetchone()[0]:
        raise RuntimeError(
            f"Role {role} has superuser, createrole, createdb, bypassrls or replication; remove them first "
            "(as a superuser) or choose another APP_DB_ROLE."
        )
    # Handing the role table ownership needs SET on it. PostgreSQL 16+ makes the creator a member with
    # ADMIN but without SET, so grant SET explicitly (INHERIT FALSE: the owner gains no privileges).
    cursor.execute("SELECT pg_has_role(current_user, %s, 'SET')", [role])
    if not cursor.fetchone()[0]:
        _exec(cursor, sql.SQL("GRANT {} TO {} WITH INHERIT FALSE, SET TRUE").format(ident, owner_ident))

    has_extensions = _exists(cursor, "SELECT 1 FROM pg_namespace WHERE nspname = 'extensions'", [])
    search_path = '"$user", public, extensions' if has_extensions else '"$user", public'
    _exec(cursor, sql.SQL("ALTER ROLE {} SET search_path = {}").format(ident, sql.SQL(search_path)))
    _exec(cursor, sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), ident))

    # public: data only, never DDL.
    _exec(cursor, sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(ident))
    _exec(cursor, sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
    _exec(
        cursor,
        sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(ident),
    )
    _exec(
        cursor, sql.SQL("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}").format(ident)
    )
    _exec(
        cursor,
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
        ).format(owner_ident, ident),
    )
    _exec(
        cursor,
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
            "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {}"
        ).format(owner_ident, ident),
    )
    if has_extensions:
        _exec(cursor, sql.SQL("GRANT USAGE ON SCHEMA extensions TO {}").format(ident))

    # Row level security: one permissive policy per RLS-enabled table the owner can manage.
    cursor.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND c.relrowsecurity "
        "AND pg_has_role(current_user, c.relowner, 'USAGE') "
        "AND NOT EXISTS (SELECT 1 FROM pg_policies p WHERE p.schemaname = 'public' "
        "AND p.tablename = c.relname AND p.policyname = %s)",
        [POLICY],
    )
    missing_policies = [row[0] for row in cursor.fetchall()]
    for table in missing_policies:
        _exec(
            cursor,
            sql.SQL(
                "CREATE POLICY {} ON {} AS PERMISSIVE FOR ALL TO {} USING (true) WITH CHECK (true)"
            ).format(sql.Identifier(POLICY), sql.Identifier("public", table), ident),
        )

    # viz_data: the dashboard store creates, owns and drops its tables at runtime.
    viz_tables = 0
    if _exists(cursor, "SELECT 1 FROM pg_namespace WHERE nspname = 'viz_data'", []):
        _exec(cursor, sql.SQL("GRANT USAGE, CREATE ON SCHEMA viz_data TO {}").format(ident))
        if _exists(cursor, "SELECT 1 FROM pg_roles WHERE rolname = 'jt_viz_reader'", []):
            _exec(cursor, sql.SQL("GRANT jt_viz_reader TO {}").format(ident))
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'viz_data' AND tableowner <> %s", [role]
        )
        for (table,) in cursor.fetchall():
            _exec(
                cursor, sql.SQL("ALTER TABLE {} OWNER TO {}").format(sql.Identifier("viz_data", table), ident)
            )
            viz_tables += 1

    # The session-revocation check, without the auth schema.
    if _session_function_exists(cursor):
        _exec(cursor, sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(sql.SQL(SESSION_FUNCTION), ident))

    cursor.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
    return {"tables": cursor.fetchone()[0], "policies": len(missing_policies), "viz_tables": viz_tables}


def _session_function_exists(cursor) -> bool:
    cursor.execute("SELECT to_regprocedure(%s) IS NOT NULL", [SESSION_FUNCTION])
    return bool(cursor.fetchone()[0])
