"""Historical no-op kept for the migration graph.

It once dropped/recreated analytics views around SQLite table rebuilds. JT-Code
runs only on Supabase PostgreSQL, where the views never needed rebuilding.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0005_enforce_tenant_ownership"),
        ("conversations", "0004_enforce_tenant_ownership"),
        ("conversions", "0002_enforce_tenant_ownership"),
        ("documents", "0002_enforce_tenant_ownership"),
        ("governance", "0007_drop_sqlite_analytics_views"),
        ("jobs", "0004_enforce_tenant_ownership"),
    ]
    operations: list = []
