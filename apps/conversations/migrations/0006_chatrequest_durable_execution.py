import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ai_gateway", "0008_generated_image_ownership"),
        ("conversations", "0005_phase4_conversation_runtime"),
        ("identity", "0006_seed_default_roles"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="chatrequest",
            name="cancel_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chatrequest",
            name="celery_task_id",
            field=models.CharField(blank=True, db_index=True, max_length=255),
        ),
        migrations.AddField(
            model_name="chatrequest",
            name="completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chatrequest",
            name="error_message",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="chatrequest",
            name="last_retry_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chatrequest",
            name="max_retries",
            field=models.PositiveIntegerField(default=3),
        ),
        migrations.AddField(
            model_name="chatrequest",
            name="model_name",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="chatrequest",
            name="model_run",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="chat_requests",
                to="ai_gateway.modelrun",
            ),
        ),
        migrations.AddField(
            model_name="chatrequest",
            name="provider_name",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="chatrequest",
            name="retry_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="chatrequest",
            name="started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="chatrequest",
            index=models.Index(fields=["celery_task_id"], name="conversatio_celery__b90071_idx"),
        ),
    ]
