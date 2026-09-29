from importlib import import_module

from django.db import migrations


def drop_views_for_sqlite_table_rebuilds(apps, schema_editor):  # noqa: ARG001
    if schema_editor.connection.vendor != "sqlite":
        return
    module = import_module("apps.governance.migrations.0004_read_only_analytics_views")
    module.drop_analytics_views(apps, schema_editor)


def recreate_views_for_sqlite_table_rebuilds(apps, schema_editor):  # noqa: ARG001
    if schema_editor.connection.vendor != "sqlite":
        return
    module = import_module("apps.governance.migrations.0004_read_only_analytics_views")
    module.create_analytics_views(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [("governance", "0006_backfill_legacy_tenant_rows")]
    operations = [migrations.RunPython(drop_views_for_sqlite_table_rebuilds, recreate_views_for_sqlite_table_rebuilds)]
