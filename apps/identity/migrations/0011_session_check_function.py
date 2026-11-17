"""``public.jt_code_session_active(session, user)``: the session-revocation check without ``auth`` access.

``SUPABASE_SESSION_CHECK`` asks whether a token's Supabase session is still live. The least-privilege
runtime role (``jt_code_app``, Phase 5) must not read GoTrue's ``auth`` schema (password hashes, refresh
tokens), so the question is answered by this ``SECURITY DEFINER`` function. It is owned by the migration
role, which can read ``auth``, and returns a single boolean for one session id.

PL/pgSQL bodies are resolved at call time, so the function is created everywhere, including databases
without an ``auth`` schema (tests, the native stack); it is only called when the check is enabled.
"""

from django.db import migrations

CREATE_SQL = """
CREATE OR REPLACE FUNCTION public.jt_code_session_active(p_session uuid, p_user uuid)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1
        FROM auth.sessions s
        JOIN auth.users u ON u.id = s.user_id
        WHERE s.id = p_session
          AND s.user_id = p_user
          AND (s.not_after IS NULL OR s.not_after > now())
          AND u.deleted_at IS NULL
          AND (u.banned_until IS NULL OR u.banned_until <= now())
    );
END;
$$;
REVOKE ALL ON FUNCTION public.jt_code_session_active(uuid, uuid) FROM PUBLIC;
"""

DROP_SQL = "DROP FUNCTION IF EXISTS public.jt_code_session_active(uuid, uuid);"


def create(apps, schema_editor):  # noqa: ARG001
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(CREATE_SQL)


def drop(apps, schema_editor):  # noqa: ARG001
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(DROP_SQL)


class Migration(migrations.Migration):
    dependencies = [("identity", "0010_hash_invite_tokens")]

    operations = [migrations.RunPython(create, drop)]
