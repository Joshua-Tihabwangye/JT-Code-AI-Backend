import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("conversions", "0001_initial"),
        ("governance", "0007_drop_sqlite_analytics_views"),
    ]

    operations = [
        migrations.AlterField(
            model_name="conversionjob",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="conversion_jobs",
                to="identity.organization",
            ),
        ),
    ]
