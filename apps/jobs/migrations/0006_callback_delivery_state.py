from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("jobs", "0005_durable_worker_runtime"),
    ]

    operations = [
        migrations.AddField(
            model_name="callback",
            name="delivered_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="callback",
            name="delivery_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="callback",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("delivering", "Delivering"),
                    ("delivered", "Delivered"),
                    ("failed", "Failed"),
                    ("expired", "Expired"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="callback",
            constraint=models.UniqueConstraint(
                fields=("job", "url"),
                name="uniq_callback_per_job_url",
            ),
        ),
    ]
