"""Keep Django-owned tables out of the Supabase Data API (PostgREST).

Django is the only data-access path: it connects as the database owner and
enforces tenancy in application code. Supabase's ``anon`` and ``authenticated``
roles (used with the publishable key and end-user JWTs) must therefore have no
privileges on the ``public`` schema's tables, views, sequences or functions, and
every table keeps Row Level Security enabled with no policies as a second wall.
This is a no-op on PostgreSQL servers without those Supabase roles.
"""

from django.db import migrations

DATA_API_ROLES = ("anon", "authenticated")

LOCK_DOWN_SQL = """
DO $$
DECLARE
    api_role text;
    tbl record;
BEGIN
    FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', api_role);
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', api_role);
            EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM %I', api_role);
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON TABLES FROM %I',
                current_user, api_role
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I',
                current_user, api_role
            );
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM %I',
                current_user, api_role
            );
        END IF;
    END LOOP;
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


def lock_down(apps, schema_editor):  # noqa: ARG001
    if schema_editor.connection.vendor != "postgresql":
        return
    # A raw cursor without params: psycopg would treat format()'s %I as placeholders.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(LOCK_DOWN_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ("governance", "0010_recreate_sqlite_analytics_views_after_phase5"),
        ("conversations", "0006_chatrequest_durable_execution"),
        ("events", "0005_alter_outboxevent_status"),
        ("jobs", "0006_callback_delivery_state"),
        ("knowledge", "0003_add_pgvector_embeddings"),
        ("identity", "0006_seed_default_roles"),
    ]

    # Irreversible by design: re-granting the Data API roles would re-expose data.
    operations = [migrations.RunPython(lock_down, migrations.RunPython.noop)]
