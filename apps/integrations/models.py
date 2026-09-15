import uuid
from django.conf import settings
from django.db import models


class Connector(models.Model):
    class Category(models.TextChoices):
        STORAGE = 'storage', 'Cloud Storage'
        DATABASE = 'database', 'Database'
        EMAIL = 'email', 'Email'
        CRM = 'crm', 'CRM'
        PROJECT_MANAGEMENT = 'project_management', 'Project Management'
        COMMUNICATION = 'communication', 'Communication'
        DEVELOPMENT = 'development', 'Development Tools'
        MARKETING = 'marketing', 'Marketing'
        ANALYTICS = 'analytics', 'Analytics'
        AI_ML = 'ai_ml', 'AI/ML Platforms'
        OTHER = 'other', 'Other'

    class AuthType(models.TextChoices):
        OAUTH2 = 'oauth2', 'OAuth 2.0'
        API_KEY = 'api_key', 'API Key'
        BASIC = 'basic', 'Basic Auth'
        BEARER = 'bearer', 'Bearer Token'
        CUSTOM = 'custom', 'Custom'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=30, choices=Category.choices)
    auth_type = models.CharField(max_length=20, choices=AuthType.choices)
    config_schema = models.JSONField(default=dict, blank=True)
    required_scopes = models.JSONField(default=list, blank=True)
    icon_url = models.URLField(blank=True)
    documentation_url = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False)
    supported_operations = models.JSONField(default=list, blank=True)
    rate_limits = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('category', 'name')

    def __str__(self):
        return f'{self.name} ({self.category})'


class ConnectorAccount(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        EXPIRED = 'expired', 'Expired'
        REVOKED = 'revoked', 'Revoked'
        ERROR = 'error', 'Error'
        PENDING = 'pending', 'Pending Authorization'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='connector_accounts'
    )
    connector = models.ForeignKey(Connector, on_delete=models.CASCADE, related_name='accounts')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='connector_accounts'
    )
    name = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    credentials_ref = models.CharField(max_length=255, blank=True)
    encrypted_config = models.JSONField(default=dict, blank=True)
    scopes_granted = models.JSONField(default=list, blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    is_default = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('organization', 'connector')),
            models.Index(fields=('user', 'connector')),
            models.Index(fields=('status',)),
        ]

    def __str__(self):
        return f'{self.name} ({self.connector.name}) - {self.status}'


class Webhook(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        PAUSED = 'paused', 'Paused'
        DISABLED = 'disabled', 'Disabled'
        ERROR = 'error', 'Error'

    class EventFilter(models.TextChoices):
        ALL = 'all', 'All Events'
        CUSTOM = 'custom', 'Custom Filter'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='webhooks'
    )
    name = models.CharField(max_length=255)
    url = models.URLField()
    secret = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    event_filter = models.CharField(max_length=20, choices=EventFilter.choices, default=EventFilter.ALL)
    events = models.JSONField(default=list, blank=True)
    headers = models.JSONField(default=dict, blank=True)
    retry_policy = models.JSONField(default=dict, blank=True)
    is_verified = models.BooleanField(default=False)
    last_triggered_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    failure_count = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='created_webhooks',
        null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('organization', 'status')),
        ]

    def __str__(self):
        return f'{self.name} - {self.status}'


class WebhookDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        DELIVERED = 'delivered', 'Delivered'
        FAILED = 'failed', 'Failed'
        RETRYING = 'retrying', 'Retrying'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    webhook = models.ForeignKey(Webhook, on_delete=models.CASCADE, related_name='deliveries')
    event_type = models.CharField(max_length=100)
    payload = models.JSONField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    attempt = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    response_status = models.PositiveIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    response_headers = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('webhook', '-created_at')),
            models.Index(fields=('status', 'next_retry_at')),
        ]

    def __str__(self):
        return f'{self.webhook} - {self.event_type} ({self.status})'


class APIKey(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        REVOKED = 'revoked', 'Revoked'
        EXPIRED = 'expired', 'Expired'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='api_keys'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='api_keys'
    )
    name = models.CharField(max_length=255)
    prefix = models.CharField(max_length=20)
    key_hash = models.CharField(max_length=128)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    scopes = models.JSONField(default=list, blank=True)
    rate_limit = models.PositiveIntegerField(default=1000)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('organization', 'status')),
            models.Index(fields=('prefix',)),
        ]

    def __str__(self):
        return f'{self.name} ({self.prefix}...) - {self.status}'


class KafkaConsumer(models.Model):
    class Status(models.TextChoices):
        RUNNING = 'running', 'Running'
        STOPPED = 'stopped', 'Stopped'
        ERROR = 'error', 'Error'
        REBALANCING = 'rebalancing', 'Rebalancing'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='kafka_consumers'
    )
    name = models.CharField(max_length=255)
    group_id = models.CharField(max_length=255)
    topics = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.STOPPED)
    config = models.JSONField(default=dict, blank=True)
    assigned_partitions = models.JSONField(default=dict, blank=True)
    lag = models.PositiveBigIntegerField(default=0)
    last_poll_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='created_kafka_consumers',
        null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('organization', 'group_id')),
        ]

    def __str__(self):
        return f'{self.name} ({self.group_id}) - {self.status}'