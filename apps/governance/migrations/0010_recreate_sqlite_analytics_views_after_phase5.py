"""Recreate SQLite analytics views after Phase 5 rebuilds the durable job table."""

from importlib import import_module

from django.db import migrations


def recreate_views(apps, schema_editor):  # noqa: ARG001
    if schema_editor.connection.vendor != "sqlite":
        return
    module = import_module("apps.governance.migrations.0004_read_only_analytics_views")
    module.create_analytics_views(apps, schema_editor)


def drop_views(apps, schema_editor):  # noqa: ARG001
    if schema_editor.connection.vendor != "sqlite":
        return
    module = import_module("apps.governance.migrations.0004_read_only_analytics_views")
    module.drop_analytics_views(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("governance", "0009_drop_sqlite_analytics_views_for_phase5"),
        ("jobs", "0005_durable_worker_runtime"),
    ]
    operations = [migrations.RunPython(recreate_views, drop_views)]
