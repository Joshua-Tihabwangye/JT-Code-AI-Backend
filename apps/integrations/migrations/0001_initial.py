# Generated migration for integrations app

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('identity', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Connector',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('slug', models.SlugField(unique=True)),
                ('name', models.CharField(max_length=100)),
                ('description', models.TextField(blank=True)),
                ('category', models.CharField(choices=[('storage', 'Cloud Storage'), ('database', 'Database'), ('email', 'Email'), ('crm', 'CRM'), ('project_management', 'Project Management'), ('communication', 'Communication'), ('development', 'Development Tools'), ('marketing', 'Marketing'), ('analytics', 'Analytics'), ('ai_ml', 'AI/ML Platforms'), ('other', 'Other')], max_length=30)),
                ('auth_type', models.CharField(choices=[('oauth2', 'OAuth 2.0'), ('api_key', 'API Key'), ('basic', 'Basic Auth'), ('bearer', 'Bearer Token'), ('custom', 'Custom')], max_length=20)),
                ('config_schema', models.JSONField(blank=True, default=dict)),
                ('required_scopes', models.JSONField(blank=True, default=list)),
                ('icon_url', models.URLField(blank=True)),
                ('documentation_url', models.URLField(blank=True)),
                ('is_active', models.BooleanField(default=True)),
                ('is_verified', models.BooleanField(default=False)),
                ('supported_operations', models.JSONField(blank=True, default=list)),
                ('rate_limits', models.JSONField(blank=True, default=dict)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ('category', 'name'),
            },
        ),
        migrations.CreateModel(
            name='ConnectorAccount',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255)),
                ('status', models.CharField(choices=[('active', 'Active'), ('expired', 'Expired'), ('revoked', 'Revoked'), ('error', 'Error'), ('pending', 'Pending Authorization')], default='pending', max_length=20)),
                ('credentials_ref', models.CharField(blank=True, max_length=255)),
                ('encrypted_config', models.JSONField(blank=True, default=dict)),
                ('scopes_granted', models.JSONField(blank=True, default=list)),
                ('token_expires_at', models.DateTimeField(blank=True, null=True)),
                ('last_sync_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True)),
                ('is_default', models.BooleanField(default=False)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('connector', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='accounts', to='integrations.connector')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='connector_accounts', to='identity.organization')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='connector_accounts', to='identity.user')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='Webhook',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255)),
                ('url', models.URLField()),
                ('secret', models.CharField(max_length=255)),
                ('status', models.CharField(choices=[('active', 'Active'), ('paused', 'Paused'), ('disabled', 'Disabled'), ('error', 'Error')], default='active', max_length=20)),
                ('event_filter', models.CharField(choices=[('all', 'All Events'), ('custom', 'Custom Filter')], default='all', max_length=20)),
                ('events', models.JSONField(blank=True, default=list)),
                ('headers', models.JSONField(blank=True, default=dict)),
                ('retry_policy', models.JSONField(blank=True, default=dict)),
                ('is_verified', models.BooleanField(default=False)),
                ('last_triggered_at', models.DateTimeField(blank=True, null=True)),
                ('last_success_at', models.DateTimeField(blank=True, null=True)),
                ('last_failure_at', models.DateTimeField(blank=True, null=True)),
                ('failure_count', models.PositiveIntegerField(default=0)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_webhooks', to='identity.user')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='webhooks', to='identity.organization')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='WebhookDelivery',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('event_type', models.CharField(max_length=100)),
                ('payload', models.JSONField()),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('delivered', 'Delivered'), ('failed', 'Failed'), ('retrying', 'Retrying')], default='pending', max_length=20)),
                ('attempt', models.PositiveIntegerField(default=0)),
                ('max_attempts', models.PositiveIntegerField(default=5)),
                ('response_status', models.PositiveIntegerField(blank=True, null=True)),
                ('response_body', models.TextField(blank=True)),
                ('response_headers', models.JSONField(blank=True, default=dict)),
                ('error_message', models.TextField(blank=True)),
                ('next_retry_at', models.DateTimeField(blank=True, null=True)),
                ('delivered_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('webhook', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='deliveries', to='integrations.webhook')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='APIKey',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255)),
                ('prefix', models.CharField(max_length=20)),
                ('key_hash', models.CharField(max_length=128)),
                ('status', models.CharField(choices=[('active', 'Active'), ('revoked', 'Revoked'), ('expired', 'Expired')], default='active', max_length=20)),
                ('scopes', models.JSONField(blank=True, default=list)),
                ('rate_limit', models.PositiveIntegerField(default=1000)),
                ('last_used_at', models.DateTimeField(blank=True, null=True)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='api_keys', to='identity.organization')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='api_keys', to='identity.user')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='KafkaConsumer',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255)),
                ('group_id', models.CharField(max_length=255)),
                ('topics', models.JSONField(blank=True, default=list)),
                ('status', models.CharField(choices=[('running', 'Running'), ('stopped', 'Stopped'), ('error', 'Error'), ('rebalancing', 'Rebalancing')], default='stopped', max_length=20)),
                ('config', models.JSONField(blank=True, default=dict)),
                ('assigned_partitions', models.JSONField(blank=True, default=dict)),
                ('lag', models.PositiveBigIntegerField(default=0)),
                ('last_poll_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_kafka_consumers', to='identity.user')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='kafka_consumers', to='identity.organization')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.AddIndex(
            model_name='connectoraccount',
            index=models.Index(fields=('organization', 'connector'), name='integrati_connec_organiz_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='connectoraccount',
            index=models.Index(fields=('user', 'connector'), name='integrati_connec_user__abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='connectoraccount',
            index=models.Index(fields=('status',), name='integrati_connec_status_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='webhook',
            index=models.Index(fields=('organization', 'status'), name='integrati_webho_organiz_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='webhookdelivery',
            index=models.Index(fields=('webhook', '-created_at'), name='integrati_webho_webhoo_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='webhookdelivery',
            index=models.Index(fields=('status', 'next_retry_at'), name='integrati_webho_status_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='apikey',
            index=models.Index(fields=('organization', 'status'), name='integrati_apike_organiz_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='apikey',
            index=models.Index(fields=('prefix',), name='integrati_apike_prefix_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='kafkaconsumer',
            index=models.Index(fields=('organization', 'group_id'), name='integrati_kafkac_organiz_abc123_idx'),
        ),
    ]