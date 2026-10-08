import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations"
    )
    organization = models.ForeignKey(
        "identity.Organization",
        on_delete=models.CASCADE,
        related_name="conversations",
    )
    title = models.CharField(max_length=255, default="New conversation")
    # The model alias the client selected for this conversation (display/default only).
    model = models.CharField(max_length=100, blank=True)
    pinned = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.title} ({self.owner})"


class Message(models.Model):
    class Role(models.TextChoices):
        SYSTEM = "system", "System"
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"
        TOOL = "tool", "Tool"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.role}: {self.content[:64]}"


class ChatRequest(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chat_requests"
    )
    organization = models.ForeignKey(
        "identity.Organization",
        on_delete=models.CASCADE,
        related_name="chat_requests",
    )
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="requests")
    idempotency_key = models.CharField(max_length=255)
    task_type = models.CharField(max_length=64, default="GENERAL_QUESTION")
    request_fingerprint = models.CharField(max_length=64)
    input_text = models.TextField()
    output_text = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    trace_id = models.CharField(max_length=100, db_index=True)
    locale = models.CharField(max_length=32, blank=True)
    timezone = models.CharField(max_length=100, blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True, db_index=True)
    retry_count = models.PositiveIntegerField(default=0)
    max_retries = models.PositiveIntegerField(default=3)
    last_retry_at = models.DateTimeField(null=True, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    # Earliest time the periodic dispatcher may (re)publish a QUEUED request. It is
    # pushed forward on every publish and retry so backoff and in-flight work are
    # never duplicated by the safety-net dispatcher.
    dispatch_after = models.DateTimeField(null=True, blank=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    provider_name = models.CharField(max_length=100, blank=True)
    model_name = models.CharField(max_length=100, blank=True)
    model_run = models.ForeignKey(
        "ai_gateway.ModelRun",
        on_delete=models.SET_NULL,
        related_name="chat_requests",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "owner", "idempotency_key"),
                name="uniq_chat_org_idempotency",
            )
        ]
        indexes = [
            models.Index(fields=("owner", "-created_at")),
            models.Index(fields=("status", "-created_at")),
            models.Index(fields=("celery_task_id",)),
        ]

    def __str__(self):
        return f"{self.task_type} {self.status} ({self.owner})"


class ConversationFeedback(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversation_feedback"
    )
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="conversation_feedback"
    )
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="feedback")
    chat_request = models.ForeignKey(
        ChatRequest,
        on_delete=models.SET_NULL,
        related_name="feedback",
        null=True,
        blank=True,
    )
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "chat_request"),
                name="uniq_feedback_per_owner_chat_request",
            )
        ]
        indexes = [
            models.Index(fields=("conversation", "-created_at"), name="conversatio_convers_8994de_idx")
        ]

    def __str__(self):
        return f"Feedback {self.rating}/5 for {self.conversation_id}"
