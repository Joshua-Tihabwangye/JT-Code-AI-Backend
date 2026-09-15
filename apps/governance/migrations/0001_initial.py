# Generated migration for governance app

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('identity', '0001_initial'),
        ('jobs', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='AuditEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('category', models.CharField(choices=[('auth', 'Authentication'), ('authorization', 'Authorization'), ('data_access', 'Data Access'), ('data_modification', 'Data Modification'), ('configuration', 'Configuration'), ('billing', 'Billing'), ('security', 'Security'), ('admin', 'Administration'), ('ai_usage', 'AI Usage'), ('file_operation', 'File Operation')], max_length=30)),
                ('action', models.CharField(max_length=100)),
                ('resource_type', models.CharField(max_length=100)),
                ('resource_id', models.CharField(blank=True, max_length=255)),
                ('severity', models.CharField(choices=[('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('critical', 'Critical')], default='low', max_length=10)),
                ('description', models.TextField()),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('user_agent', models.TextField(blank=True)),
                ('trace_id', models.CharField(blank=True, db_index=True, max_length=100)),
                ('request_id', models.UUIDField(blank=True, db_index=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='audit_events', to='identity.user')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='audit_events', to='identity.organization')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='ConsentRecord',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('consent_type', models.CharField(choices=[('terms', 'Terms of Service'), ('privacy', 'Privacy Policy'), ('marketing', 'Marketing Communications'), ('analytics', 'Analytics'), ('ai_training', 'AI Training Data'), ('third_party', 'Third-party Sharing'), ('data_processing', 'Data Processing Agreement')], max_length=30)),
                ('status', models.CharField(choices=[('granted', 'Granted'), ('denied', 'Denied'), ('withdrawn', 'Withdrawn'), ('expired', 'Expired')], max_length=20)),
                ('version', models.CharField(max_length=50)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('user_agent', models.TextField(blank=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('granted_at', models.DateTimeField(blank=True, null=True)),
                ('withdrawn_at', models.DateTimeField(blank=True, null=True)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='consent_records', to='identity.organization')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='consent_records', to='identity.user')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='RetentionRule',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('data_category', models.CharField(choices=[('chat_messages', 'Chat Messages'), ('generated_files', 'Generated Files'), ('uploaded_files', 'Uploaded Files'), ('audit_events', 'Audit Events'), ('billing_records', 'Billing Records'), ('usage_logs', 'Usage Logs'), ('model_runs', 'Model Runs'), ('vector_embeddings', 'Vector Embeddings'), ('knowledge_documents', 'Knowledge Documents'), ('sentry_events', 'Sentry Events'), ('kafka_events', 'Kafka Events')], max_length=30)),
                ('action', models.CharField(choices=[('soft_delete', 'Soft Delete'), ('hard_delete', 'Hard Delete'), ('anonymize', 'Anonymize'), ('archive', 'Archive to Cold Storage')], max_length=20)),
                ('retention_days', models.PositiveIntegerField()),
                ('grace_period_days', models.PositiveIntegerField(default=30)),
                ('is_active', models.BooleanField(default=True)),
                ('legal_hold', models.BooleanField(default=False)),
                ('description', models.TextField(blank=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_retention_rules', to='identity.user')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='retention_rules', to='identity.organization')),
            ],
            options={
                'ordering': ('data_category',),
            },
        ),
        migrations.CreateModel(
            name='SafetyEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('category', models.CharField(choices=[('prompt_injection', 'Prompt Injection'), ('toxic_content', 'Toxic Content'), ('pii_detected', 'PII Detected'), ('copyright', 'Copyright Violation'), ('malware', 'Malware Detection'), ('unsafe_output', 'Unsafe Model Output'), ('jailbreak', 'Jailbreak Attempt'), ('policy_violation', 'Policy Violation')], max_length=30)),
                ('severity', models.CharField(choices=[('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('critical', 'Critical')], max_length=10)),
                ('action_taken', models.CharField(choices=[('logged', 'Logged Only'), ('blocked', 'Blocked'), ('quarantined', 'Quarantined'), ('flagged_review', 'Flagged for Review'), ('user_notified', 'User Notified'), ('account_suspended', 'Account Suspended')], default='logged', max_length=20)),
                ('description', models.TextField()),
                ('evidence', models.JSONField(blank=True, default=dict)),
                ('model_provider', models.CharField(blank=True, max_length=100)),
                ('model_name', models.CharField(blank=True, max_length=100)),
                ('prompt_hash', models.CharField(blank=True, max_length=64)),
                ('response_hash', models.CharField(blank=True, max_length=64)),
                ('trace_id', models.CharField(blank=True, db_index=True, max_length=100)),
                ('request_id', models.UUIDField(blank=True, db_index=True, null=True)),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('review_notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('job', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='safety_events', to='jobs.job')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='safety_events', to='identity.organization')),
                ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reviewed_safety_events', to='identity.user')),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='safety_events', to='identity.user')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='SupportCase',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('status', models.CharField(choices=[('open', 'Open'), ('in_progress', 'In Progress'), ('waiting_user', 'Waiting for User'), ('waiting_third_party', 'Waiting for Third Party'), ('resolved', 'Resolved'), ('closed', 'Closed')], default='open', max_length=20)),
                ('priority', models.CharField(choices=[('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('urgent', 'Urgent')], default='medium', max_length=10)),
                ('category', models.CharField(choices=[('billing', 'Billing'), ('technical', 'Technical Issue'), ('account', 'Account Access'), ('feature', 'Feature Request'), ('bug', 'Bug Report'), ('security', 'Security Concern'), ('compliance', 'Compliance'), ('other', 'Other')], max_length=20)),
                ('subject', models.CharField(max_length=255)),
                ('description', models.TextField()),
                ('tags', models.JSONField(blank=True, default=list)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('closed_at', models.DateTimeField(blank=True, null=True)),
                ('first_response_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('assigned_to', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='assigned_support_cases', to='identity.user')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='support_cases', to='identity.organization')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='support_cases', to='identity.user')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.AddConstraint(
            model_name='consentrecord',
            constraint=models.UniqueConstraint(fields=('user', 'consent_type', 'version'), name='uniq_consent_user_type_version'),
        ),
        migrations.AddConstraint(
            model_name='retentionrule',
            constraint=models.UniqueConstraint(fields=('organization', 'data_category'), name='governance_retention_org_category_uniq'),
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=('organization', '-created_at'), name='governance_aud_organiz_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=('actor', '-created_at'), name='governance_aud_actor__abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=('category', '-created_at'), name='governance_aud_catego_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=('resource_type', 'resource_id'), name='governance_aud_resour_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='consentrecord',
            index=models.Index(fields=('organization', 'user', 'consent_type'), name='governance_con_organiz_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='consentrecord',
            index=models.Index(fields=('status',), name='governance_con_status_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='safetyevent',
            index=models.Index(fields=('organization', '-created_at'), name='governance_saf_organiz_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='safetyevent',
            index=models.Index(fields=('user', '-created_at'), name='governance_saf_user__abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='safetyevent',
            index=models.Index(fields=('category', '-created_at'), name='governance_saf_catego_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='safetyevent',
            index=models.Index(fields=('severity', '-created_at'), name='governance_saf_severi_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='safetyevent',
            index=models.Index(fields=('action_taken',), name='governance_saf_action_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='supportcase',
            index=models.Index(fields=('organization', '-created_at'), name='governance_sup_organiz_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='supportcase',
            index=models.Index(fields=('user', '-created_at'), name='governance_sup_user__abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='supportcase',
            index=models.Index(fields=('status', 'priority'), name='governance_sup_status_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='supportcase',
            index=models.Index(fields=('assigned_to', 'status'), name='governance_sup_assigne_abc123_idx'),
        ),
    ]