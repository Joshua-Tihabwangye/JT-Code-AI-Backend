import uuid

from django.conf import settings
from django.db import models


class Job(models.Model):
    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        VALIDATING = 'validating', 'Validating'
        RUNNING = 'running', 'Running'
        WAITING_APPROVAL = 'waiting_approval', 'Waiting for Approval'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'
        EXPIRED = 'expired', 'Expired'

    class TaskType(models.TextChoices):
        GENERAL_QUESTION = 'GENERAL_QUESTION', 'General Question'
        IMAGE_UNDERSTANDING = 'IMAGE_UNDERSTANDING', 'Image Understanding'
        IMAGE_GENERATION = 'IMAGE_GENERATION', 'Image Generation'
        DOCUMENT_DRAFTING = 'DOCUMENT_DRAFTING', 'Document Drafting'
        DOCUMENT_RENDERING = 'DOCUMENT_RENDERING', 'Document Rendering'
        FILE_CONVERSION = 'FILE_CONVERSION', 'File Conversion'
        SEARCH_RESEARCH = 'SEARCH_RESEARCH', 'Search & Research'
        RAG_QUERY = 'RAG_QUERY', 'RAG Query'
        KNOWLEDGE_INGESTION = 'KNOWLEDGE_INGESTION', 'Knowledge Ingestion'
        SCHEDULED_AUTOMATION = 'SCHEDULED_AUTOMATION', 'Scheduled Automation'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='jobs'
    )
    organization = models.ForeignKey(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='jobs',
        null=True,
        blank=True
    )
    conversation = models.ForeignKey(
        'conversations.Conversation',
        on_delete=models.SET_NULL,
        related_name='jobs',
        null=True,
        blank=True
    )
    request_id = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    idempotency_key = models.CharField(max_length=255, db_index=True, default=uuid.uuid4)
    task_type = models.CharField(max_length=50, choices=TaskType.choices)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.QUEUED)
    input_payload = models.JSONField()
    entitlement_snapshot = models.JSONField(default=dict, blank=True)
    reserved_credits = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    actual_credits = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    result = models.JSONField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    trace_id = models.CharField(max_length=100, db_index=True)
    n8n_workflow_id = models.CharField(max_length=100, blank=True)
    n8n_execution_id = models.CharField(max_length=100, blank=True)
    callback_url = models.URLField(blank=True)
    deadline = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('owner', '-created_at')),
            models.Index(fields=('organization', '-created_at')),
            models.Index(fields=('status', '-created_at')),
            models.Index(fields=('task_type', '-created_at')),
            models.Index(fields=('request_id',)),
            models.Index(fields=('idempotency_key',)),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=('owner', 'idempotency_key'),
                name='uniq_job_idempotency'
            ),
        ]

    def __str__(self):
        return f'{self.task_type} ({self.status}) - {self.id}'


class JobStep(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'
        SKIPPED = 'skipped', 'Skipped'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='steps')
    name = models.CharField(max_length=100)
    step_order = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    input_payload = models.JSONField(default=dict, blank=True)
    output_payload = models.JSONField(null=True, blank=True)
    provider = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    estimated_cost_usd = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    actual_cost_usd = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('step_order',)
        indexes = [
            models.Index(fields=('job', 'step_order')),
        ]

    def __str__(self):
        return f'{self.job_id} - {self.name} ({self.status})'


class ProviderAttempt(models.Model):
    class Status(models.TextChoices):
        ATTEMPTING = 'attempting', 'Attempting'
        SUCCESS = 'success', 'Success'
        FAILED = 'failed', 'Failed'
        FALLBACK = 'fallback', 'Fallback'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job_step = models.ForeignKey(JobStep, on_delete=models.CASCADE, related_name='provider_attempts')
    attempt_number = models.PositiveIntegerField()
    provider = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=Status.choices)
    input_payload = models.JSONField(default=dict, blank=True)
    output_payload = models.JSONField(null=True, blank=True)
    input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    policy_version = models.CharField(max_length=50, blank=True)
    trace_id = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('attempt_number',)
        indexes = [
            models.Index(fields=('job_step', 'attempt_number')),
        ]

    def __str__(self):
        return f'{self.job_step_id} - Attempt {self.attempt_number} ({self.status})'


class WorkflowRun(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.OneToOneField(Job, on_delete=models.CASCADE, related_name='workflow_run')
    n8n_workflow_id = models.CharField(max_length=100)
    n8n_execution_id = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    input_payload = models.JSONField()
    output_payload = models.JSONField(null=True, blank=True)
    steps_completed = models.PositiveIntegerField(default=0)
    total_steps = models.PositiveIntegerField(default=0)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f'WorkflowRun {self.id} - {self.status}'


class Callback(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        DELIVERED = 'delivered', 'Delivered'
        FAILED = 'failed', 'Failed'
        EXPIRED = 'expired', 'Expired'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='callbacks')
    url = models.URLField()
    payload = models.JSONField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    response_status = models.PositiveIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('status', 'next_retry_at')),
            models.Index(fields=('job',)),
        ]

    def __str__(self):
        return f'Callback {self.id} - {self.status}'