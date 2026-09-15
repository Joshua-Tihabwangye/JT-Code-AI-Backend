import uuid
from django.conf import settings
from django.db import models

class Asset(models.Model):
    class Status(models.TextChoices):
        READY = 'ready', 'Ready'
        QUARANTINED = 'quarantined', 'Quarantined'
        DELETED = 'deleted', 'Deleted'
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='assets')
    cloudinary_public_id = models.CharField(max_length=500, unique=True)
    secure_url = models.URLField(max_length=1000)
    resource_type = models.CharField(max_length=50)
    format = models.CharField(max_length=50, blank=True)
    bytes = models.PositiveBigIntegerField(default=0)
    version = models.PositiveBigIntegerField(default=0)
    original_filename = models.CharField(max_length=500)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.READY)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=('owner', '-created_at'))]
