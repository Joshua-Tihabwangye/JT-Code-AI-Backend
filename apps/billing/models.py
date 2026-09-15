import uuid
from django.conf import settings
from django.db import models


class Plan(models.Model):
    class Interval(models.TextChoices):
        MONTHLY = 'monthly', 'Monthly'
        YEARLY = 'yearly', 'Yearly'
        ONCE = 'once', 'One-time'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        ARCHIVED = 'archived', 'Archived'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    price_cents = models.PositiveIntegerField(default=0)
    currency = models.CharField(max_length=3, default='USD')
    interval = models.CharField(max_length=20, choices=Interval.choices, default=Interval.MONTHLY)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    features = models.JSONField(default=dict, blank=True)
    limits = models.JSONField(default=dict, blank=True)
    credit_value_usd = models.DecimalField(max_digits=10, decimal_places=6, default=1)
    monthly_credits = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    is_popular = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('sort_order', 'name')

    def __str__(self):
        return f'{self.name} (${self.price_cents/100:.2f}/{self.interval})'


class Entitlement(models.Model):
    class FeatureType(models.TextChoices):
        CHAT_MESSAGES = 'chat_messages', 'Chat Messages'
        IMAGE_GENERATIONS = 'image_generations', 'Image Generations'
        DOCUMENT_RENDERS = 'document_renders', 'Document Renders'
        FILE_CONVERSIONS = 'file_conversions', 'File Conversions'
        KNOWLEDGE_COLLECTIONS = 'knowledge_collections', 'Knowledge Collections'
        KNOWLEDGE_DOCUMENTS = 'knowledge_documents', 'Knowledge Documents'
        SEARCH_QUERIES = 'search_queries', 'Search Queries'
        RAG_QUERIES = 'rag_queries', 'RAG Queries'
        API_CALLS = 'api_calls', 'API Calls'
        STORAGE_GB = 'storage_gb', 'Storage (GB)'
        WORKFLOW_EXECUTIONS = 'workflow_executions', 'Workflow Executions'
        PRIORITY_SUPPORT = 'priority_support', 'Priority Support'
        CUSTOM_MODELS = 'custom_models', 'Custom Models'
        SSO = 'sso', 'SSO'
        AUDIT_LOGS = 'audit_logs', 'Audit Logs'

    class LimitType(models.TextChoices):
        HARD = 'hard', 'Hard Limit'
        SOFT = 'soft', 'Soft Limit (with overage)'
        UNLIMITED = 'unlimited', 'Unlimited'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name='entitlements')
    feature = models.CharField(max_length=50, choices=FeatureType.choices)
    limit_type = models.CharField(max_length=20, choices=LimitType.choices, default=LimitType.HARD)
    limit_value = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    reset_period = models.CharField(max_length=20, default='monthly')
    overage_price_cents = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('plan', 'feature')
        ordering = ('feature',)

    def __str__(self):
        return f'{self.plan.name} - {self.get_feature_display()}: {self.limit_value or "unlimited"}'


class Subscription(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        PAST_DUE = 'past_due', 'Past Due'
        CANCELED = 'canceled', 'Canceled'
        INCOMPLETE = 'incomplete', 'Incomplete'
        TRIALING = 'trialing', 'Trialing'
        PAUSED = 'paused', 'Paused'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='subscriptions'
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name='subscriptions')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INCOMPLETE)
    provider = models.CharField(max_length=50, default='stripe')
    provider_subscription_id = models.CharField(max_length=255, unique=True)
    provider_customer_id = models.CharField(max_length=255, blank=True)
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()
    cancel_at_period_end = models.BooleanField(default=False)
    canceled_at = models.DateTimeField(null=True, blank=True)
    trial_start = models.DateTimeField(null=True, blank=True)
    trial_end = models.DateTimeField(null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('organization', 'status')),
            models.Index(fields=('provider_subscription_id',)),
        ]

    def __str__(self):
        return f'{self.organization} - {self.plan.name} ({self.status})'


class CreditWallet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='credit_wallet'
    )
    balance = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    reserved_balance = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    currency = models.CharField(max_length=3, default='USD')
    credit_value_usd = models.DecimalField(max_digits=10, decimal_places=6, default=1)
    auto_topup_enabled = models.BooleanField(default=False)
    auto_topup_threshold = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    auto_topup_amount = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    last_topup_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=('organization',)),
        ]

    def __str__(self):
        return f'{self.organization} - Balance: {self.balance} credits'

    @property
    def available_balance(self):
        return self.balance - self.reserved_balance


class CreditLedger(models.Model):
    class Direction(models.TextChoices):
        DEBIT = 'debit', 'Debit'
        CREDIT = 'credit', 'Credit'

    class Reason(models.TextChoices):
        SUBSCRIPTION_GRANT = 'subscription_grant', 'Subscription Grant'
        MANUAL_TOPUP = 'manual_topup', 'Manual Top-up'
        AUTO_TOPUP = 'auto_topup', 'Auto Top-up'
        USAGE_CHAT = 'usage_chat', 'Chat Usage'
        USAGE_IMAGE_GEN = 'usage_image_gen', 'Image Generation'
        USAGE_DOC_RENDER = 'usage_doc_render', 'Document Rendering'
        USAGE_FILE_CONV = 'usage_file_conv', 'File Conversion'
        USAGE_SEARCH = 'usage_search', 'Search'
        USAGE_RAG = 'usage_rag', 'RAG Query'
        USAGE_WORKFLOW = 'usage_workflow', 'Workflow Execution'
        USAGE_STORAGE = 'usage_storage', 'Storage'
        REFUND = 'refund', 'Refund'
        ADJUSTMENT = 'adjustment', 'Adjustment'
        EXPIRED = 'expired', 'Expired Credits'
        PROMOTION = 'promotion', 'Promotional Credits'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(CreditWallet, on_delete=models.CASCADE, related_name='ledger_entries')
    direction = models.CharField(max_length=10, choices=Direction.choices)
    credits = models.DecimalField(max_digits=20, decimal_places=6)
    reason = models.CharField(max_length=30, choices=Reason.choices)
    description = models.TextField(blank=True)
    request_id = models.UUIDField(null=True, blank=True, db_index=True)
    job_id = models.UUIDField(null=True, blank=True, db_index=True)
    idempotency_key = models.CharField(max_length=255, db_index=True)
    price_snapshot = models.JSONField(default=dict, blank=True)
    balance_after = models.DecimalField(max_digits=20, decimal_places=6)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('wallet', '-created_at')),
            models.Index(fields=('wallet', 'idempotency_key')),
            models.Index(fields=('request_id',)),
            models.Index(fields=('job_id',)),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=('wallet', 'idempotency_key'),
                name='uniq_ledger_idempotency'
            ),
        ]

    def __str__(self):
        return f'{self.wallet} - {self.direction} {self.credits} ({self.reason})'


class Invoice(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        OPEN = 'open', 'Open'
        PAID = 'paid', 'Paid'
        VOID = 'void', 'Void'
        UNCOLLECTIBLE = 'uncollectible', 'Uncollectible'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='invoices'
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.SET_NULL,
        related_name='invoices',
        null=True,
        blank=True
    )
    provider = models.CharField(max_length=50, default='stripe')
    provider_invoice_id = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    amount_cents = models.PositiveIntegerField(default=0)
    amount_paid_cents = models.PositiveIntegerField(default=0)
    currency = models.CharField(max_length=3, default='USD')
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    due_date = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    invoice_pdf_url = models.URLField(blank=True)
    hosted_invoice_url = models.URLField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('organization', '-created_at')),
            models.Index(fields=('provider_invoice_id',)),
        ]

    def __str__(self):
        return f'Invoice {self.provider_invoice_id} - {self.amount_cents/100:.2f} {self.currency} ({self.status})'


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PROCESSING = 'processing', 'Processing'
        SUCCEEDED = 'succeeded', 'Succeeded'
        FAILED = 'failed', 'Failed'
        CANCELED = 'canceled', 'Canceled'
        REFUNDED = 'refunded', 'Refunded'

    class Type(models.TextChoices):
        SUBSCRIPTION = 'subscription', 'Subscription'
        TOPUP = 'topup', 'Credit Top-up'
        ONE_TIME = 'one_time', 'One-time'
        REFUND = 'refund', 'Refund'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='payments'
    )
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.SET_NULL,
        related_name='payments',
        null=True,
        blank=True
    )
    wallet = models.ForeignKey(
        CreditWallet,
        on_delete=models.SET_NULL,
        related_name='payments',
        null=True,
        blank=True
    )
    provider = models.CharField(max_length=50, default='stripe')
    provider_payment_id = models.CharField(max_length=255, unique=True)
    type = models.CharField(max_length=20, choices=Type.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    amount_cents = models.PositiveIntegerField(default=0)
    currency = models.CharField(max_length=3, default='USD')
    credits_granted = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    idempotency_key = models.CharField(max_length=255, db_index=True)
    failure_code = models.CharField(max_length=100, blank=True)
    failure_message = models.TextField(blank=True)
    receipt_url = models.URLField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    succeeded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('organization', '-created_at')),
            models.Index(fields=('provider_payment_id',)),
            models.Index(fields=('idempotency_key',)),
        ]

    def __str__(self):
        return f'Payment {self.provider_payment_id} - {self.amount_cents/100:.2f} {self.currency} ({self.status})'