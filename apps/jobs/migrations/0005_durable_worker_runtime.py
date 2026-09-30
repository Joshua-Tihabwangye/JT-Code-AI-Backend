from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("jobs", "0004_enforce_tenant_ownership"), ("governance", "0009_drop_sqlite_analytics_views_for_phase5")]

    operations = [
        migrations.AddField(
            model_name="job",
            name="queue_name",
            field=models.CharField(db_index=True, default="jobs.default", max_length=64),
        ),
        migrations.AddField(
            model_name="job",
            name="celery_task_id",
            field=models.CharField(blank=True, db_index=True, max_length=255),
        ),
        migrations.AddField(
            model_name="job",
            name="progress_percent",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="job",
            name="retry_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="job",
            name="max_retries",
            field=models.PositiveIntegerField(default=3),
        ),
        migrations.AddField(
            model_name="job",
            name="last_retry_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="job",
            name="cancel_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="job",
            index=models.Index(
                fields=["queue_name", "status", "-created_at"], name="jobs_job_queue_n_48ca10_idx"
            ),
        ),
    ]
