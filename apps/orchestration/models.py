"""Django-owned state for n8n orchestration (Phase 16).

n8n never holds canonical state. Every workflow definition, dispatch attempt,
event delivery, accepted callback and tenant automation lives here; n8n only
receives signed requests and reports back through signed callbacks that Django
validates against this state.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class WorkflowDefinition(models.Model):
    """One immutable version of a workflow from ``n8n/workflows/<key>.v<version>.json``."""

    class Kind(models.TextChoices):
        JOB = "job", "Job workflow"
        EVENT = "event", "Event-triggered workflow"
        REQUEST = "request", "Synchronous request"
        ERROR = "error", "Error workflow"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.SlugField(max_length=100)
    version = models.PositiveIntegerField()
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    definition = models.JSONField()
    checksum = models.CharField(max_length=64)
    webhook_path = models.CharField(max_length=255, blank=True)
    task_types = models.JSONField(default=list, blank=True)
    event_types = models.JSONField(default=list, blank=True)
    timeout_seconds = models.PositiveIntegerField(default=1800)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    required_credentials = models.JSONField(default=list, blank=True)
    # The version Django dispatches to for this key (exactly one per key).
    is_active = models.BooleanField(default=True)
    n8n_workflow_id = models.CharField(max_length=64, blank=True)
    n8n_active = models.BooleanField(default=False)
    synced_checksum = models.CharField(max_length=64, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)
    sync_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("key", "-version")
        constraints = [
            models.UniqueConstraint(fields=("key", "version"), name="uniq_workflow_key_version"),
            models.UniqueConstraint(
                fields=("key",), condition=models.Q(is_active=True), name="uniq_active_workflow_per_key"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.key} v{self.version}"


class WorkflowEventDelivery(models.Model):
    """A domain event (outbox row) delivered to a subscribed n8n workflow."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DELIVERING = "delivering", "Delivering"
        ACCEPTED = "accepted", "Accepted by n8n"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    definition = models.ForeignKey(WorkflowDefinition, on_delete=models.PROTECT, related_name="deliveries")
    event_id = models.UUIDField()
    event_type = models.CharField(max_length=255)
    organization = models.ForeignKey(
        "identity.Organization",
        on_delete=models.CASCADE,
        related_name="workflow_deliveries",
        null=True,
        blank=True,
    )
    payload = models.JSONField(default=dict)
    headers = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    deadline_at = models.DateTimeField(null=True, blank=True)
    n8n_execution_id = models.CharField(max_length=64, blank=True)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=("definition", "event_id"), name="uniq_delivery_per_event"),
        ]
        indexes = [
            models.Index(fields=("status", "next_attempt_at"), name="orch_delivery_due_idx"),
            models.Index(fields=("event_type", "-created_at"), name="orch_delivery_type_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.event_type} -> {self.definition_id} ({self.status})"


class WorkflowCallback(models.Model):
    """Every accepted signed callback, keyed by its nonce: durable exactly-once processing."""

    nonce = models.CharField(max_length=128, unique=True)
    kind = models.CharField(max_length=40)
    target_id = models.CharField(max_length=64, blank=True)
    outcome = models.CharField(max_length=40, blank=True)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self) -> str:
        return f"{self.kind}:{self.target_id}"


class Automation(models.Model):
    """A tenant's scheduled n8n automation; each run is a Django job."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="automations"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="automations"
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    workflow_key = models.SlugField(max_length=100, default="scheduled-automation")
    schedule = models.CharField(max_length=100)
    input = models.JSONField(default=dict)
    is_active = models.BooleanField(default=True)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_job = models.ForeignKey(
        "jobs.Job", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    last_error = models.TextField(blank=True)
    run_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("is_active", "next_run_at"), name="orch_automation_due_idx")]

    def __str__(self) -> str:
        return self.name
