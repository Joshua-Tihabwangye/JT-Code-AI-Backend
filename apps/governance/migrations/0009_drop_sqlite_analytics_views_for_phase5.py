"""Drop SQLite analytics views before Phase 5 rebuilds the durable job table."""

from importlib import import_module

from django.db import migrations


def drop_views(apps, schema_editor):  # noqa: ARG001
    if schema_editor.connection.vendor != "sqlite":
        return
    module = import_module("apps.governance.migrations.0004_read_only_analytics_views")
    module.drop_analytics_views(apps, schema_editor)


def recreate_views(apps, schema_editor):  # noqa: ARG001
    if schema_editor.connection.vendor != "sqlite":
        return
    module = import_module("apps.governance.migrations.0004_read_only_analytics_views")
    module.create_analytics_views(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [("governance", "0008_recreate_sqlite_analytics_views")]
    operations = [migrations.RunPython(drop_views, recreate_views)]
