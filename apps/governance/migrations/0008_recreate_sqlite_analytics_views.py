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
        ("assets", "0005_enforce_tenant_ownership"),
        ("conversations", "0004_enforce_tenant_ownership"),
        ("conversions", "0002_enforce_tenant_ownership"),
        ("documents", "0002_enforce_tenant_ownership"),
        ("governance", "0007_drop_sqlite_analytics_views"),
        ("jobs", "0004_enforce_tenant_ownership"),
    ]
    operations = [migrations.RunPython(recreate_views, drop_views)]
