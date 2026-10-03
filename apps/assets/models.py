import uuid

from django.conf import settings
from django.db import models


class Asset(models.Model):
    class Status(models.TextChoices):
        READY = "ready", "Ready"
        QUARANTINED = "quarantined", "Quarantined"
        DELETED = "deleted", "Deleted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="assets")
    organization = models.ForeignKey(
        "identity.Organization",
        on_delete=models.CASCADE,
        related_name="assets",
    )
    imagekit_file_id = models.CharField(max_length=500, unique=True)
    imagekit_file_path = models.CharField(max_length=1000, blank=True)
    secure_url = models.URLField(max_length=1000)
    resource_type = models.CharField(max_length=50)
    format = models.CharField(max_length=50, blank=True)
    bytes = models.PositiveBigIntegerField(default=0)
    version = models.PositiveBigIntegerField(default=0)
    original_filename = models.CharField(max_length=500)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.READY)
    metadata = models.JSONField(default=dict, blank=True)
    checksum_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    provider_fingerprint = models.CharField(max_length=64, blank=True, db_index=True)
    provenance = models.JSONField(default=dict, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    provider_deleted_at = models.DateTimeField(null=True, blank=True)
    deletion_error = models.TextField(blank=True)
    deletion_attempts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=("owner", "-created_at")),
            models.Index(fields=("organization", "-created_at")),
        ]

    def __str__(self):
        return f"{self.original_filename} ({self.resource_type}) - {self.status}"


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
    imagekit_file_id = models.CharField(max_length=500, blank=True)
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
