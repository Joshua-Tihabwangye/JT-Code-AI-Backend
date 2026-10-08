import uuid

from django.conf import settings
from django.db import models


class Asset(models.Model):
    class Status(models.TextChoices):
        READY = "ready", "Ready"
        QUARANTINED = "quarantined", "Quarantined"
        DELETED = "deleted", "Deleted"

    class Visibility(models.TextChoices):
        # Owner and organization admins only. Consumers (knowledge, analytics,
        # documents) expose derived content under their own access policies.
        PRIVATE = "private", "Private"
        ORGANIZATION = "organization", "Organization"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Assets belong to the organization; removing a user must not delete them.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="assets", null=True, blank=True
    )
    organization = models.ForeignKey(
        "identity.Organization",
        on_delete=models.CASCADE,
        related_name="assets",
    )
    name = models.CharField(max_length=500, blank=True)
    # Supabase Storage object identity.  ``storage_key`` is always tenant
    # prefixed and the bucket is private; public URLs are never persisted.
    storage_object_id = models.CharField(max_length=1000, unique=True)
    storage_key = models.CharField(max_length=1000, blank=True)
    storage_bucket = models.CharField(max_length=100, default="jt-code-assets")
    storage_url = models.URLField(max_length=1000, blank=True)
    resource_type = models.CharField(max_length=50)
    format = models.CharField(max_length=50, blank=True)
    bytes = models.PositiveBigIntegerField(default=0)
    version = models.PositiveBigIntegerField(default=0)
    original_filename = models.CharField(max_length=500)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.READY)
    visibility = models.CharField(
        max_length=20, choices=Visibility.choices, default=Visibility.PRIVATE, db_index=True
    )
    metadata = models.JSONField(default=dict, blank=True)
    checksum_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    provider_fingerprint = models.CharField(max_length=64, blank=True, db_index=True)
    provenance = models.JSONField(default=dict, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    provider_deleted_at = models.DateTimeField(null=True, blank=True)
    deletion_error = models.TextField(blank=True)
    deletion_attempts = models.PositiveIntegerField(default=0)
    last_verified_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=("owner", "-created_at")),
            models.Index(fields=("organization", "-created_at")),
        ]

    def __str__(self):
        return f"{self.original_filename} ({self.resource_type}) - {self.status}"

    @property
    def display_name(self) -> str:
        return self.name or self.original_filename

    @property
    def content_type(self) -> str:
        return str((self.metadata or {}).get("content_type") or "application/octet-stream")


class UploadIntent(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETED = "completed", "Completed"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="asset_upload_intents"
    )
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="asset_upload_intents"
    )
    token = models.CharField(max_length=100, unique=True)
    folder = models.CharField(max_length=1000)
    file_name = models.CharField(max_length=500)
    original_filename = models.CharField(max_length=500)
    content_type = models.CharField(max_length=255)
    expected_bytes = models.PositiveBigIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    storage_object_key = models.CharField(max_length=1000, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=("owner", "status", "expires_at"), name="assets_upl_owner_i_21004c_idx"),
            models.Index(fields=("organization", "status"), name="assets_upl_organiz_3a50a0_idx"),
        ]

    def __str__(self):
        return f"Upload {self.id} ({self.status})"


class ConversationAttachment(models.Model):
    """A file attached to a conversation by a member who can read both."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="conversation_attachments")
    conversation = models.ForeignKey(
        "conversations.Conversation", on_delete=models.CASCADE, related_name="attachments"
    )
    attached_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="+", null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("asset", "conversation"), name="uniq_conversation_attachment")
        ]

    def __str__(self):
        return f"{self.asset_id} in {self.conversation_id}"
