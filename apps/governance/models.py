import uuid
from django.conf import settings
from django.db import models


class AuditEvent(models.Model):
    class Category(models.TextChoices):
        AUTH = 'auth', 'Authentication'
        AUTHORIZATION = 'authorization', 'Authorization'
        DATA_ACCESS = 'data_access', 'Data Access'
        DATA_MODIFICATION = 'data_modification', 'Data Modification'
        CONFIGURATION = 'configuration', 'Configuration'
        BILLING = 'billing', 'Billing'
        SECURITY = 'security', 'Security'
        ADMIN = 'admin', 'Administration'
        AI_USAGE = 'ai_usage', 'AI Usage'
        FILE_OPERATION = 'file_operation', 'File Operation'

    class Severity(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'
        CRITICAL = 'critical', 'Critical'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='audit_events'
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='audit_events',
        null=True,
        blank=True
    )
    category = models.CharField(max_length=30, choices=Category.choices)
    action = models.CharField(max_length=100)
    resource_type = models.CharField(max_length=100)
    resource_id = models.CharField(max_length=255, blank=True)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.LOW)
    description = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    trace_id = models.CharField(max_length=100, blank=True, db_index=True)
    request_id = models.UUIDField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('organization', '-created_at')),
            models.Index(fields=('actor', '-created_at')),
            models.Index(fields=('category', '-created_at')),
            models.Index(fields=('resource_type', 'resource_id')),
        ]

    def __str__(self):
        return f'{self.category}.{self.action} - {self.actor} @ {self.created_at}'


class ConsentRecord(models.Model):
    class ConsentType(models.TextChoices):
        TERMS = 'terms', 'Terms of Service'
        PRIVACY = 'privacy', 'Privacy Policy'
        MARKETING = 'marketing', 'Marketing Communications'
        ANALYTICS = 'analytics', 'Analytics'
        AI_TRAINING = 'ai_training', 'AI Training Data'
        THIRD_PARTY = 'third_party', 'Third-party Sharing'
        DATA_PROCESSING = 'data_processing', 'Data Processing Agreement'

    class Status(models.TextChoices):
        GRANTED = 'granted', 'Granted'
        DENIED = 'denied', 'Denied'
        WITHDRAWN = 'withdrawn', 'Withdrawn'
        EXPIRED = 'expired', 'Expired'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='consent_records'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='consent_records'
    )
    consent_type = models.CharField(max_length=30, choices=ConsentType.choices)
    status = models.CharField(max_length=20, choices=Status.choices)
    version = models.CharField(max_length=50)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    granted_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('organization', 'user', 'consent_type')),
            models.Index(fields=('status',)),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=('user', 'consent_type', 'version'),
                name='uniq_consent_user_type_version'
            ),
        ]

    def __str__(self):
        return f'{self.user} - {self.consent_type} ({self.status})'


class RetentionRule(models.Model):
    class DataCategory(models.TextChoices):
        CHAT_MESSAGES = 'chat_messages', 'Chat Messages'
        GENERATED_FILES = 'generated_files', 'Generated Files'
        UPLOADED_FILES = 'uploaded_files', 'Uploaded Files'
        AUDIT_EVENTS = 'audit_events', 'Audit Events'
        BILLING_RECORDS = 'billing_records', 'Billing Records'
        USAGE_LOGS = 'usage_logs', 'Usage Logs'
        MODEL_RUNS = 'model_runs', 'Model Runs'
        VECTOR_EMBEDDINGS = 'vector_embeddings', 'Vector Embeddings'
        KNOWLEDGE_DOCUMENTS = 'knowledge_documents', 'Knowledge Documents'
        SENTRY_EVENTS = 'sentry_events', 'Sentry Events'
        KAFKA_EVENTS = 'kafka_events', 'Kafka Events'

    class Action(models.TextChoices):
        SOFT_DELETE = 'soft_delete', 'Soft Delete'
        HARD_DELETE = 'hard_delete', 'Hard Delete'
        ANONYMIZE = 'anonymize', 'Anonymize'
        ARCHIVE = 'archive', 'Archive to Cold Storage'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='retention_rules'
    )
    data_category = models.CharField(max_length=30, choices=DataCategory.choices)
    action = models.CharField(max_length=20, choices=Action.choices)
    retention_days = models.PositiveIntegerField()
    grace_period_days = models.PositiveIntegerField(default=30)
    is_active = models.BooleanField(default=True)
    legal_hold = models.BooleanField(default=False)
    description = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='created_retention_rules',
        null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('data_category',)
        unique_together = ('organization', 'data_category')

    def __str__(self):
        return f'{self.organization} - {self.data_category}: {self.action} after {self.retention_days} days'


class SafetyEvent(models.Model):
    class Category(models.TextChoices):
        PROMPT_INJECTION = 'prompt_injection', 'Prompt Injection'
        TOXIC_CONTENT = 'toxic_content', 'Toxic Content'
        PII_DETECTED = 'pii_detected', 'PII Detected'
        COPYRIGHT = 'copyright', 'Copyright Violation'
        MALWARE = 'malware', 'Malware Detection'
        UNSAFE_OUTPUT = 'unsafe_output', 'Unsafe Model Output'
        JAILBREAK = 'jailbreak', 'Jailbreak Attempt'
        POLICY_VIOLATION = 'policy_violation', 'Policy Violation'

    class Severity(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'
        CRITICAL = 'critical', 'Critical'

    class Action(models.TextChoices):
        LOGGED = 'logged', 'Logged Only'
        BLOCKED = 'blocked', 'Blocked'
        QUARANTINED = 'quarantined', 'Quarantined'
        FLAGGED_REVIEW = 'flagged_review', 'Flagged for Review'
        USER_NOTIFIED = 'user_notified', 'User Notified'
        ACCOUNT_SUSPENDED = 'account_suspended', 'Account Suspended'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='safety_events'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='safety_events',
        null=True,
        blank=True
    )
    job = models.ForeignKey(
        'jobs.Job',
        on_delete=models.SET_NULL,
        related_name='safety_events',
        null=True,
        blank=True
    )
    category = models.CharField(max_length=30, choices=Category.choices)
    severity = models.CharField(max_length=10, choices=Severity.choices)
    action_taken = models.CharField(max_length=20, choices=Action.choices, default=Action.LOGGED)
    description = models.TextField()
    evidence = models.JSONField(default=dict, blank=True)
    model_provider = models.CharField(max_length=100, blank=True)
    model_name = models.CharField(max_length=100, blank=True)
    prompt_hash = models.CharField(max_length=64, blank=True)
    response_hash = models.CharField(max_length=64, blank=True)
    trace_id = models.CharField(max_length=100, blank=True, db_index=True)
    request_id = models.UUIDField(null=True, blank=True, db_index=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='reviewed_safety_events',
        null=True,
        blank=True
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('organization', '-created_at')),
            models.Index(fields=('user', '-created_at')),
            models.Index(fields=('category', '-created_at')),
            models.Index(fields=('severity', '-created_at')),
            models.Index(fields=('action_taken',)),
        ]

    def __str__(self):
        return f'{self.category} ({self.severity}) - {self.action_taken}'


class SupportCase(models.Model):
    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        IN_PROGRESS = 'in_progress', 'In Progress'
        WAITING_USER = 'waiting_user', 'Waiting for User'
        WAITING_THIRD_PARTY = 'waiting_third_party', 'Waiting for Third Party'
        RESOLVED = 'resolved', 'Resolved'
        CLOSED = 'closed', 'Closed'

    class Priority(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'
        URGENT = 'urgent', 'Urgent'

    class Category(models.TextChoices):
        BILLING = 'billing', 'Billing'
        TECHNICAL = 'technical', 'Technical Issue'
        ACCOUNT = 'account', 'Account Access'
        FEATURE = 'feature', 'Feature Request'
        BUG = 'bug', 'Bug Report'
        SECURITY = 'security', 'Security Concern'
        COMPLIANCE = 'compliance', 'Compliance'
        OTHER = 'other', 'Other'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='support_cases'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='support_cases'
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    category = models.CharField(max_length=20, choices=Category.choices)
    subject = models.CharField(max_length=255)
    description = models.TextField()
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='assigned_support_cases',
        null=True,
        blank=True
    )
    tags = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    first_response_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('organization', '-created_at')),
            models.Index(fields=('user', '-created_at')),
            models.Index(fields=('status', 'priority')),
            models.Index(fields=('assigned_to', 'status')),
        ]

    def __str__(self):
        return f'{self.subject} ({self.status}) - {self.organization}'