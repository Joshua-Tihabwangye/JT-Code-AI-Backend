from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class Dataset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="datasets"
    )
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="datasets")
    asset = models.ForeignKey(
        "assets.Asset", null=True, blank=True, on_delete=models.SET_NULL, related_name="datasets"
    )
    name = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100, default="text/csv")
    # Small CSVs can be retained for deterministic, isolated worker execution.
    # Larger datasets must use a registered tenant-owned Asset.
    inline_data = models.TextField(blank=True)
    schema = models.JSONField(default=dict, blank=True)
    row_count = models.PositiveIntegerField(default=0)
    byte_size = models.PositiveBigIntegerField(default=0)
    source_checksum_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    is_shared = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("organization", "-created_at"),
                name="analytics_d_organiz_7d242d_idx",
            )
        ]

    def __str__(self):
        return self.name


class DatasetGrant(models.Model):
    class Permission(models.TextChoices):
        VIEW = "view", "View"
        ANALYZE = "analyze", "Analyze"

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="grants")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="dataset_grants"
    )
    permission = models.CharField(max_length=12, choices=Permission.choices, default=Permission.VIEW)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("dataset", "user"), name="uniq_dataset_grant")]

    def __str__(self):
        return f"{self.dataset_id}:{self.user_id}"


class AnalysisRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name="analysis_runs")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="analysis_runs"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    transform = models.JSONField(default=dict, blank=True)
    profile = models.JSONField(default=dict, blank=True)
    result_schema = models.JSONField(default=dict, blank=True)
    result_preview = models.JSONField(default=list, blank=True)
    result_asset = models.ForeignKey(
        "assets.Asset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analysis_results",
    )
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("dataset", "-created_at"),
                name="analytics_a_dataset_2d02d7_idx",
            ),
            models.Index(
                fields=("status", "created_at"),
                name="analytics_a_status_5e32e7_idx",
            ),
        ]

    def __str__(self):
        return f"Analysis {self.id} ({self.status})"


class Visualization(models.Model):
    class Kind(models.TextChoices):
        BAR = "bar", "Bar"
        LINE = "line", "Line"
        SCATTER = "scatter", "Scatter"
        HISTOGRAM = "histogram", "Histogram"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    analysis_run = models.ForeignKey(AnalysisRun, on_delete=models.CASCADE, related_name="visualizations")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    x_column = models.CharField(max_length=255)
    y_column = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    plotly_spec = models.JSONField(default=dict, blank=True)
    artifact_asset = models.ForeignKey(
        "assets.Asset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analytics_visualizations",
    )
    artifact_checksum_sha256 = models.CharField(max_length=64, blank=True)
    result_schema = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("analysis_run", "-created_at"),
                name="analytics_v_analysi_5b15c4_idx",
            )
        ]

    def __str__(self):
        return f"{self.kind} chart {self.id}"
