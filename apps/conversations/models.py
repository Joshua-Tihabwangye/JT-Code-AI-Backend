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
    trace_id = models.CharField(max_length=100, db_index=True)
    locale = models.CharField(max_length=32, blank=True)
    timezone = models.CharField(max_length=100, blank=True)
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
