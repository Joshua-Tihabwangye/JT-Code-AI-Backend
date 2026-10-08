"""Usage metering records.

``UsageReservation`` is a credit hold taken *before* billable work starts.
``UsageRecord`` is the immutable result of settling one (PostgreSQL triggers
reject UPDATE and DELETE; see migration 0002). ``UsageReconciliation`` compares
recorded provider usage with what was billed.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class Feature(models.TextChoices):
    """Billable/quota-able features (shared with ``billing.Entitlement``)."""

    CHAT_MESSAGES = "chat_messages", "Chat Messages"
    RAG_QUERIES = "rag_queries", "RAG Queries"
    SEARCH_QUERIES = "search_queries", "Search Queries"
    KNOWLEDGE_DOCUMENTS = "knowledge_documents", "Knowledge Documents"
    IMAGE_GENERATIONS = "image_generations", "Image Generations"
    DOCUMENT_RENDERS = "document_renders", "Document Renders"
    FILE_CONVERSIONS = "file_conversions", "File Conversions"
    WORKFLOW_EXECUTIONS = "workflow_executions", "Workflow Executions"
    AGENT_RUNS = "agent_runs", "Agent Runs"
    ANALYSIS_RUNS = "analysis_runs", "Analysis Runs"
    API_CALLS = "api_calls", "API Calls"


class UsageReservation(models.Model):
    class Status(models.TextChoices):
        HELD = "held", "Held"
        SETTLED = "settled", "Settled"
        RELEASED = "released", "Released"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="usage_reservations"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="+", null=True, blank=True
    )
    feature = models.CharField(max_length=40, choices=Feature.choices)
    quantity = models.PositiveIntegerField(default=1)
    credits_reserved = models.DecimalField(max_digits=20, decimal_places=6)
    source_type = models.CharField(max_length=40)
    source_id = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.HELD)
    expires_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("source_type", "source_id"), name="uniq_reservation_source"),
        ]
        indexes = [
            models.Index(fields=("status", "expires_at"), name="usage_res_status_expiry_idx"),
            models.Index(fields=("organization", "feature", "status"), name="usage_res_org_feature_idx"),
        ]

    def __str__(self):
        return f"{self.feature} hold {self.credits_reserved} ({self.status})"


class UsageRecord(models.Model):
    """Immutable settled usage (append-only; enforced in the database)."""

    class Basis(models.TextChoices):
        PROVIDER_COST = "provider_cost", "Provider cost"
        FLAT = "flat", "Flat price"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="usage_records"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="+", null=True, blank=True
    )
    reservation = models.OneToOneField(
        UsageReservation, on_delete=models.SET_NULL, related_name="record", null=True, blank=True
    )
    feature = models.CharField(max_length=40, choices=Feature.choices)
    quantity = models.PositiveIntegerField(default=1)
    source_type = models.CharField(max_length=40)
    source_id = models.CharField(max_length=64)
    basis = models.CharField(max_length=20, choices=Basis.choices)
    credits_charged = models.DecimalField(max_digits=20, decimal_places=6)
    # Cost above the reservation that was *not* charged (overspend prevention).
    credits_uncollected = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    provider_cost_usd = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    model_run_count = models.PositiveIntegerField(default=0)
    pricing = models.JSONField(default=dict, blank=True)
    period = models.CharField(max_length=7, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("source_type", "source_id"), name="uniq_usage_record_source"),
        ]
        indexes = [
            models.Index(fields=("organization", "period", "feature"), name="usage_rec_org_period_idx"),
        ]

    def __str__(self):
        return f"{self.feature} {self.credits_charged} credits ({self.period})"


class UsageReconciliation(models.Model):
    """Daily comparison of recorded provider usage with billed usage."""

    class Status(models.TextChoices):
        OK = "ok", "Consistent"
        DRIFT = "drift", "Drift detected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    date = models.DateField()
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="usage_reconciliations", null=True
    )
    provider = models.CharField(max_length=60)
    model_runs = models.PositiveIntegerField(default=0)
    model_run_cost_usd = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    recomputed_cost_usd = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    billed_cost_usd = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    unbilled_runs = models.PositiveIntegerField(default=0)
    unbilled_cost_usd = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    provider_reported_cost_usd = models.DecimalField(max_digits=20, decimal_places=8, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OK)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("date", "organization", "provider"), name="uniq_reconciliation_day"
            ),
        ]
        ordering = ("-date", "provider")

    def __str__(self):
        return f"{self.date} {self.provider} {self.status}"


class CostAnomaly(models.Model):
    """An hour whose provider cost is far above its trailing baseline (Phase 19).

    ``organization`` is null for the platform-wide series.
    """

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization",
        on_delete=models.CASCADE,
        related_name="cost_anomalies",
        null=True,
        blank=True,
    )
    hour = models.DateTimeField()
    observed_usd = models.DecimalField(max_digits=20, decimal_places=8)
    expected_usd = models.DecimalField(max_digits=20, decimal_places=8)
    stddev_usd = models.DecimalField(max_digits=20, decimal_places=8)
    zscore = models.FloatField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-hour",)
        constraints = [
            models.UniqueConstraint(fields=("organization", "hour"), name="uniq_cost_anomaly_org_hour"),
            models.UniqueConstraint(
                fields=("hour",),
                condition=models.Q(organization__isnull=True),
                name="uniq_cost_anomaly_platform_hour",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.organization_id or 'platform'} {self.hour:%Y-%m-%d %H}:00 z={self.zscore:.1f}"
