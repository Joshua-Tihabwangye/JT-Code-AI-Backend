import uuid

from django.conf import settings
from django.db import models


class ConversionJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='conversion_jobs',
    )
    organization = models.ForeignKey(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='conversion_jobs',
        null=True,
        blank=True,
    )
    input_filename = models.CharField(max_length=500)
    input_format = models.CharField(max_length=20)
    output_format = models.CharField(max_length=20)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    input_bytes = models.PositiveBigIntegerField(default=0)
    output_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    reserved_credits = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    input_path = models.CharField(max_length=1000, blank=True)
    output_path = models.CharField(max_length=1000, blank=True)
    output_url = models.URLField(max_length=1000, blank=True)
    options = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=('owner', '-created_at'))]

    def __str__(self):
        return f'{self.input_format}->{self.output_format} ({self.status})'
