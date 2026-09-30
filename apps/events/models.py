import uuid

from django.db import models


class OutboxEvent(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PUBLISHING = "publishing", "Publishing"
        PUBLISHED = "published", "Published"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    topic = models.CharField(max_length=255)
    event_key = models.CharField(max_length=255)
    payload = models.JSONField()
    headers = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(auto_now_add=True)
    publishing_started_at = models.DateTimeField(null=True, blank=True)
    publishing_token = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=("status", "available_at", "created_at"))]

    def __str__(self):
        return f"{self.topic} [{self.status}] key={self.event_key}"


class ConsumedEvent(models.Model):
    """Idempotency ledger for a consumer group after a handler commits successfully."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    consumer_group = models.CharField(max_length=255)
    event_id = models.UUIDField()
    event_type = models.CharField(max_length=255)
    topic = models.CharField(max_length=255)
    partition = models.IntegerField()
    offset = models.BigIntegerField()
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("consumer_group", "event_id"),
                name="uniq_consumed_event_group_id",
            ),
        ]
        indexes = [models.Index(fields=("consumer_group", "-processed_at"))]

    def __str__(self):
        return f"{self.consumer_group}: {self.event_type} ({self.event_id})"


class DeadLetterEvent(models.Model):
    """A rejected consumer event, retained for diagnosis and explicit replay."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    consumer_group = models.CharField(max_length=255)
    event_id = models.CharField(max_length=64, blank=True)
    event_type = models.CharField(max_length=255, blank=True)
    topic = models.CharField(max_length=255)
    partition = models.IntegerField(null=True, blank=True)
    offset = models.BigIntegerField(null=True, blank=True)
    payload = models.JSONField(default=dict)
    headers = models.JSONField(default=dict, blank=True)
    error = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    replayed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("consumer_group", "-created_at"))]
        constraints = [
            models.UniqueConstraint(
                fields=("consumer_group", "topic", "partition", "offset"),
                name="uniq_dead_letter_source_offset",
            ),
        ]

    def __str__(self):
        return f"DLQ {self.topic} ({self.consumer_group})"
