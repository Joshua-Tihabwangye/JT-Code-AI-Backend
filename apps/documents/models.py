from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class Document(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        RENDERING = 'rendering', 'Rendering'
        READY = 'ready', 'Ready'
        FAILED = 'failed', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='documents',
    )
    organization = models.ForeignKey(
        'identity.Organization',
        on_delete=models.CASCADE,
        related_name='documents',
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=500)
    template = models.CharField(max_length=100, default='general')
    template_version = models.PositiveIntegerField(default=1)
    content = models.TextField(blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.DRAFT)
    version = models.PositiveIntegerField(default=1)
    provenance = models.JSONField(default=dict, blank=True)
    download_url = models.URLField(blank=True)
    page_count = models.PositiveIntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['owner', 'status']),
            models.Index(fields=['organization', 'status']),
        ]

    def __str__(self) -> str:
        return self.title