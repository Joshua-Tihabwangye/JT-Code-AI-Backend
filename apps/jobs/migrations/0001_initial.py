# Generated migration for jobs app
# Run: python manage.py makemigrations jobs
# Then: python manage.py migrate

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('identity', '0001_initial'),
        ('conversations', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Job',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('request_id', models.UUIDField(default=uuid.uuid4, editable=False)),
                ('idempotency_key', models.CharField(db_index=True, max_length=255)),
                ('task_type', models.CharField(choices=[('GENERAL_QUESTION', 'General Question'), ('IMAGE_UNDERSTANDING', 'Image Understanding'), ('IMAGE_GENERATION', 'Image Generation'), ('DOCUMENT_DRAFTING', 'Document Drafting'), ('DOCUMENT_RENDERING', 'Document Rendering'), ('FILE_CONVERSION', 'File Conversion'), ('SEARCH_RESEARCH', 'Search & Research'), ('RAG_QUERY', 'RAG Query'), ('KNOWLEDGE_INGESTION', 'Knowledge Ingestion'), ('SCHEDULED_AUTOMATION', 'Scheduled Automation')], max_length=50)),
                ('status', models.CharField(choices=[('queued', 'Queued'), ('validating', 'Validating'), ('running', 'Running'), ('waiting_approval', 'Waiting for Approval'), ('completed', 'Completed'), ('failed', 'Failed'), ('cancelled', 'Cancelled'), ('expired', 'Expired')], default='queued', max_length=30)),
                ('input_payload', models.JSONField()),
                ('entitlement_snapshot', models.JSONField(blank=True, default=dict)),
                ('reserved_credits', models.DecimalField(decimal_places=6, default=0, max_digits=20)),
                ('actual_credits', models.DecimalField(blank=True, decimal_places=6, max_digits=20, null=True)),
                ('result', models.JSONField(blank=True, null=True)),
                ('error_code', models.CharField(blank=True, max_length=100)),
                ('error_message', models.TextField(blank=True)),
                ('trace_id', models.CharField(blank=True, db_index=True, max_length=100)),
                ('n8n_workflow_id', models.CharField(blank=True, max_length=100)),
                ('n8n_execution_id', models.CharField(blank=True, max_length=100)),
                ('callback_url', models.URLField(blank=True)),
                ('deadline', models.DateTimeField(blank=True, null=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('conversation', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='jobs', to='conversations.conversation')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='jobs', to='identity.organization')),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='jobs', to='identity.user')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='JobStep',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=100)),
                ('step_order', models.PositiveIntegerField()),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed'), ('skipped', 'Skipped')], default='pending', max_length=20)),
                ('input_payload', models.JSONField(blank=True, default=dict)),
                ('output_payload', models.JSONField(blank=True, null=True)),
                ('provider', models.CharField(blank=True, max_length=100)),
                ('model', models.CharField(blank=True, max_length=100)),
                ('estimated_cost_usd', models.DecimalField(decimal_places=8, default=0, max_digits=20)),
                ('actual_cost_usd', models.DecimalField(blank=True, decimal_places=8, max_digits=20, null=True)),
                ('input_tokens', models.PositiveBigIntegerField(default=0)),
                ('output_tokens', models.PositiveBigIntegerField(default=0)),
                ('error_message', models.TextField(blank=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('job', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='steps', to='jobs.job')),
            ],
            options={
                'ordering': ('step_order',),
            },
        ),
        migrations.CreateModel(
            name='ProviderAttempt',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('attempt_number', models.PositiveIntegerField()),
                ('provider', models.CharField(max_length=100)),
                ('model', models.CharField(max_length=100)),
                ('status', models.CharField(choices=[('attempting', 'Attempting'), ('success', 'Success'), ('failed', 'Failed'), ('fallback', 'Fallback')], max_length=20)),
                ('input_payload', models.JSONField(blank=True, default=dict)),
                ('output_payload', models.JSONField(blank=True, null=True)),
                ('input_tokens', models.PositiveBigIntegerField(default=0)),
                ('output_tokens', models.PositiveBigIntegerField(default=0)),
                ('cost_usd', models.DecimalField(decimal_places=8, default=0, max_digits=20)),
                ('latency_ms', models.PositiveIntegerField(blank=True, null=True)),
                ('error_code', models.CharField(blank=True, max_length=100)),
                ('error_message', models.TextField(blank=True)),
                ('policy_version', models.CharField(blank=True, max_length=50)),
                ('trace_id', models.CharField(blank=True, max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('job_step', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='provider_attempts', to='jobs.jobstep')),
            ],
            options={
                'ordering': ('attempt_number',),
            },
        ),
        migrations.CreateModel(
            name='WorkflowRun',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('n8n_workflow_id', models.CharField(max_length=100)),
                ('n8n_execution_id', models.CharField(blank=True, max_length=100)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed'), ('cancelled', 'Cancelled')], default='pending', max_length=20)),
                ('input_payload', models.JSONField()),
                ('output_payload', models.JSONField(blank=True, null=True)),
                ('steps_completed', models.PositiveIntegerField(default=0)),
                ('total_steps', models.PositiveIntegerField(default=0)),
                ('progress_percent', models.PositiveSmallIntegerField(default=0)),
                ('error_message', models.TextField(blank=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('job', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='workflow_run', to='jobs.job')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='Callback',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('url', models.URLField()),
                ('payload', models.JSONField()),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('delivered', 'Delivered'), ('failed', 'Failed'), ('expired', 'Expired')], default='pending', max_length=20)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('max_attempts', models.PositiveIntegerField(default=5)),
                ('last_attempt_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True)),
                ('response_status', models.PositiveIntegerField(blank=True, null=True)),
                ('response_body', models.TextField(blank=True)),
                ('next_retry_at', models.DateTimeField(blank=True, null=True)),
                ('expires_at', models.DateTimeField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('job', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='callbacks', to='jobs.job')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.AddConstraint(
            model_name='job',
            constraint=models.UniqueConstraint(fields=('owner', 'idempotency_key'), name='uniq_job_idempotency'),
        ),
        migrations.AddIndex(
            model_name='job',
            index=models.Index(fields=('owner', '-created_at'), name='jobs_job_owner_cr_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='job',
            index=models.Index(fields=('organization', '-created_at'), name='jobs_job_organiz_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='job',
            index=models.Index(fields=('status', '-created_at'), name='jobs_job_status__abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='job',
            index=models.Index(fields=('task_type', '-created_at'), name='jobs_job_task_ty_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='job',
            index=models.Index(fields=('request_id',), name='jobs_job_request_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='job',
            index=models.Index(fields=('idempotency_key',), name='jobs_job_idempot_abc123_idx'),
        ),
    ]