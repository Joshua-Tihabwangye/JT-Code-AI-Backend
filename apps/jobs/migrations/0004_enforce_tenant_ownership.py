import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("jobs", "0003_alter_job_idempotency_key"),
        ("governance", "0007_drop_sqlite_analytics_views"),
    ]

    operations = [
        migrations.AlterField(
            model_name="job",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="jobs",
                to="identity.organization",
            ),
        ),
    ]
