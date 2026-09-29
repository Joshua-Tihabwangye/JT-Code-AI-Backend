import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0001_initial"),
        ("governance", "0007_drop_sqlite_analytics_views"),
    ]

    operations = [
        migrations.AlterField(
            model_name="document",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="documents",
                to="identity.organization",
            ),
        ),
    ]
