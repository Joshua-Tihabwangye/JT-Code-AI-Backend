"""Provision the PostgreSQL group role used by analytics consumers."""

from django.db import migrations

ANALYTICS_ROLE = "jt_code_analytics_reader"
ANALYTICS_VIEWS = (
    "analytics_job_summary",
    "analytics_usage_ledger",
    "analytics_billing_summary",
    "analytics_conversation_summary",
    "analytics_asset_summary",
)


def provision_analytics_role(apps, schema_editor):  # noqa: ARG001
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jt_code_analytics_reader') THEN
                    CREATE ROLE jt_code_analytics_reader
                        NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
                END IF;
            END
            $$;
            """
        )
        for view_name in ANALYTICS_VIEWS:
            cursor.execute(f"REVOKE ALL ON TABLE {view_name} FROM PUBLIC")  # nosec B608 -- constant DDL.
            cursor.execute(  # nosec B608 -- constant DDL.
                f"GRANT SELECT ON TABLE {view_name} TO {ANALYTICS_ROLE}"
            )


def revoke_analytics_role_grants(apps, schema_editor):  # noqa: ARG001
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for view_name in ANALYTICS_VIEWS:
            cursor.execute(  # nosec B608 -- constant DDL.
                f"REVOKE SELECT ON TABLE {view_name} FROM {ANALYTICS_ROLE}"
            )


class Migration(migrations.Migration):
    dependencies = [("governance", "0004_read_only_analytics_views")]

    operations = [migrations.RunPython(provision_analytics_role, revoke_analytics_role_grants)]
