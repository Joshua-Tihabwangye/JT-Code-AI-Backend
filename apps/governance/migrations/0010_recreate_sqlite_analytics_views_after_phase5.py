"""Historical no-op kept for the migration graph.

It once dropped/recreated analytics views around SQLite table rebuilds. JT-Code
runs only on Supabase PostgreSQL, where the views never needed rebuilding.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("governance", "0009_drop_sqlite_analytics_views_for_phase5"),
        ("jobs", "0005_durable_worker_runtime"),
    ]
    operations: list = []
