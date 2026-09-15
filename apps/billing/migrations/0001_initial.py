# Generated migration for billing app

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
            name='Plan',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=100)),
                ('slug', models.SlugField(unique=True)),
                ('description', models.TextField(blank=True)),
                ('price_cents', models.PositiveIntegerField(default=0)),
                ('currency', models.CharField(default='USD', max_length=3)),
                ('interval', models.CharField(choices=[('monthly', 'Monthly'), ('yearly', 'Yearly'), ('once', 'One-time')], default='monthly', max_length=20)),
                ('status', models.CharField(choices=[('active', 'Active'), ('archived', 'Archived')], default='active', max_length=20)),
                ('features', models.JSONField(blank=True, default=dict)),
                ('limits', models.JSONField(blank=True, default=dict)),
                ('credit_value_usd', models.DecimalField(decimal_places=6, default=1, max_digits=10)),
                ('monthly_credits', models.DecimalField(decimal_places=6, default=0, max_digits=20)),
                ('is_popular', models.BooleanField(default=False)),
                ('sort_order', models.PositiveIntegerField(default=0)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ('sort_order', 'name'),
            },
        ),
        migrations.CreateModel(
            name='Entitlement',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('feature', models.CharField(choices=[('chat_messages', 'Chat Messages'), ('image_generations', 'Image Generations'), ('document_renders', 'Document Renders'), ('file_conversions', 'File Conversions'), ('knowledge_collections', 'Knowledge Collections'), ('knowledge_documents', 'Knowledge Documents'), ('search_queries', 'Search Queries'), ('rag_queries', 'RAG Queries'), ('api_calls', 'API Calls'), ('storage_gb', 'Storage (GB)'), ('workflow_executions', 'Workflow Executions'), ('priority_support', 'Priority Support'), ('custom_models', 'Custom Models'), ('sso', 'SSO'), ('audit_logs', 'Audit Logs')], max_length=50)),
                ('limit_type', models.CharField(choices=[('hard', 'Hard Limit'), ('soft', 'Soft Limit (with overage)'), ('unlimited', 'Unlimited')], default='hard', max_length=20)),
                ('limit_value', models.DecimalField(blank=True, decimal_places=6, max_digits=20, null=True)),
                ('reset_period', models.CharField(default='monthly', max_length=20)),
                ('overage_price_cents', models.PositiveIntegerField(default=0)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('plan', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='entitlements', to='billing.plan')),
            ],
            options={
                'ordering': ('feature',),
            },
        ),
        migrations.CreateModel(
            name='Subscription',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('status', models.CharField(choices=[('active', 'Active'), ('past_due', 'Past Due'), ('canceled', 'Canceled'), ('incomplete', 'Incomplete'), ('trialing', 'Trialing'), ('paused', 'Paused')], default='incomplete', max_length=20)),
                ('provider', models.CharField(default='stripe', max_length=50)),
                ('provider_subscription_id', models.CharField(max_length=255, unique=True)),
                ('provider_customer_id', models.CharField(blank=True, max_length=255)),
                ('current_period_start', models.DateTimeField()),
                ('current_period_end', models.DateTimeField()),
                ('cancel_at_period_end', models.BooleanField(default=False)),
                ('canceled_at', models.DateTimeField(blank=True, null=True)),
                ('trial_start', models.DateTimeField(blank=True, null=True)),
                ('trial_end', models.DateTimeField(blank=True, null=True)),
                ('quantity', models.PositiveIntegerField(default=1)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='subscriptions', to='identity.organization')),
                ('plan', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='subscriptions', to='billing.plan')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='CreditWallet',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('balance', models.DecimalField(decimal_places=6, default=0, max_digits=20)),
                ('reserved_balance', models.DecimalField(decimal_places=6, default=0, max_digits=20)),
                ('currency', models.CharField(default='USD', max_length=3)),
                ('credit_value_usd', models.DecimalField(decimal_places=6, default=1, max_digits=10)),
                ('auto_topup_enabled', models.BooleanField(default=False)),
                ('auto_topup_threshold', models.DecimalField(decimal_places=6, default=0, max_digits=20)),
                ('auto_topup_amount', models.DecimalField(decimal_places=6, default=0, max_digits=20)),
                ('last_topup_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='credit_wallet', to='identity.organization')),
            ],
        ),
        migrations.CreateModel(
            name='CreditLedger',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('direction', models.CharField(choices=[('debit', 'Debit'), ('credit', 'Credit')], max_length=10)),
                ('credits', models.DecimalField(decimal_places=6, max_digits=20)),
                ('reason', models.CharField(choices=[('subscription_grant', 'Subscription Grant'), ('manual_topup', 'Manual Top-up'), ('auto_topup', 'Auto Top-up'), ('usage_chat', 'Chat Usage'), ('usage_image_gen', 'Image Generation'), ('usage_doc_render', 'Document Rendering'), ('usage_file_conv', 'File Conversion'), ('usage_search', 'Search'), ('usage_rag', 'RAG Query'), ('usage_workflow', 'Workflow Execution'), ('usage_storage', 'Storage'), ('refund', 'Refund'), ('adjustment', 'Adjustment'), ('expired', 'Expired Credits'), ('promotion', 'Promotional Credits')], max_length=30)),
                ('description', models.TextField(blank=True)),
                ('request_id', models.UUIDField(blank=True, db_index=True, null=True)),
                ('job_id', models.UUIDField(blank=True, db_index=True, null=True)),
                ('idempotency_key', models.CharField(db_index=True, max_length=255)),
                ('price_snapshot', models.JSONField(blank=True, default=dict)),
                ('balance_after', models.DecimalField(decimal_places=6, max_digits=20)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('wallet', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ledger_entries', to='billing.creditwallet')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='Invoice',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('provider', models.CharField(default='stripe', max_length=50)),
                ('provider_invoice_id', models.CharField(max_length=255, unique=True)),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('open', 'Open'), ('paid', 'Paid'), ('void', 'Void'), ('uncollectible', 'Uncollectible')], default='draft', max_length=20)),
                ('amount_cents', models.PositiveIntegerField(default=0)),
                ('amount_paid_cents', models.PositiveIntegerField(default=0)),
                ('currency', models.CharField(default='USD', max_length=3)),
                ('period_start', models.DateTimeField()),
                ('period_end', models.DateTimeField()),
                ('due_date', models.DateTimeField(blank=True, null=True)),
                ('paid_at', models.DateTimeField(blank=True, null=True)),
                ('invoice_pdf_url', models.URLField(blank=True)),
                ('hosted_invoice_url', models.URLField(blank=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='invoices', to='identity.organization')),
                ('subscription', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='invoices', to='billing.subscription')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='Payment',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('provider', models.CharField(default='stripe', max_length=50)),
                ('provider_payment_id', models.CharField(max_length=255, unique=True)),
                ('type', models.CharField(choices=[('subscription', 'Subscription'), ('topup', 'Credit Top-up'), ('one_time', 'One-time'), ('refund', 'Refund')], max_length=20)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('processing', 'Processing'), ('succeeded', 'Succeeded'), ('failed', 'Failed'), ('canceled', 'Canceled'), ('refunded', 'Refunded')], default='pending', max_length=20)),
                ('amount_cents', models.PositiveIntegerField(default=0)),
                ('currency', models.CharField(default='USD', max_length=3)),
                ('credits_granted', models.DecimalField(decimal_places=6, default=0, max_digits=20)),
                ('idempotency_key', models.CharField(db_index=True, max_length=255)),
                ('failure_code', models.CharField(blank=True, max_length=100)),
                ('failure_message', models.TextField(blank=True)),
                ('receipt_url', models.URLField(blank=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('succeeded_at', models.DateTimeField(blank=True, null=True)),
                ('invoice', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='payments', to='billing.invoice')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='payments', to='identity.organization')),
                ('wallet', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='payments', to='billing.creditwallet')),
            ],
            options={
                'ordering': ('-created_at',),
            },
        ),
        migrations.AddConstraint(
            model_name='entitlement',
            constraint=models.UniqueConstraint(fields=('plan', 'feature'), name='billing_entitlement_plan_feature_uniq'),
        ),
        migrations.AddConstraint(
            model_name='creditledger',
            constraint=models.UniqueConstraint(fields=('wallet', 'idempotency_key'), name='uniq_ledger_idempotency'),
        ),
        migrations.AddIndex(
            model_name='subscription',
            index=models.Index(fields=('organization', 'status'), name='billing_sub_organiz_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='subscription',
            index=models.Index(fields=('provider_subscription_id',), name='billing_sub_provide_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='creditledger',
            index=models.Index(fields=('wallet', '-created_at'), name='billing_cre_wallet__abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='creditledger',
            index=models.Index(fields=('wallet', 'idempotency_key'), name='billing_cre_wallet__abc123_2'),
        ),
        migrations.AddIndex(
            model_name='creditledger',
            index=models.Index(fields=('request_id',), name='billing_cre_request_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='creditledger',
            index=models.Index(fields=('job_id',), name='billing_cre_job_id__abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='invoice',
            index=models.Index(fields=('organization', '-created_at'), name='billing_inv_organiz_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='invoice',
            index=models.Index(fields=('provider_invoice_id',), name='billing_inv_provide_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='payment',
            index=models.Index(fields=('organization', '-created_at'), name='billing_pay_organiz_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='payment',
            index=models.Index(fields=('provider_payment_id',), name='billing_pay_provide_abc123_idx'),
        ),
        migrations.AddIndex(
            model_name='payment',
            index=models.Index(fields=('idempotency_key',), name='billing_pay_idempot_abc123_idx'),
        ),
    ]