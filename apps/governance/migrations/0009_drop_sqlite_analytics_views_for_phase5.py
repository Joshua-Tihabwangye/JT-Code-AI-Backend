"""Historical no-op kept for the migration graph.

It once dropped/recreated analytics views around SQLite table rebuilds. JT-Code
runs only on Supabase PostgreSQL, where the views never needed rebuilding.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("governance", "0008_recreate_sqlite_analytics_views")]
    operations: list = []
