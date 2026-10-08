"""Durable evidence for production verification (Phases 18-19)."""

from __future__ import annotations

import uuid

from django.db import models


class VerificationRun(models.Model):
    """One drill, load test or evaluation with its measured result.

    ``manage.py release_gate`` requires a recent passing run of every required
    kind before a release; the summary is the evidence kept with it.
    """

    class Kind(models.TextChoices):
        RESTORE_DRILL = "restore_drill", "Database restore drill"
        DLQ_DRILL = "dlq_drill", "Dead-letter recovery drill"
        SATURATION_DRILL = "saturation_drill", "Celery/Kafka saturation drill"
        LOAD_TEST = "load_test", "Load test"
        SPIKE_TEST = "spike_test", "Spike test"
        SOAK_TEST = "soak_test", "Soak test"
        STREAMING_TEST = "streaming_test", "SSE streaming concurrency test"
        RAG_EVALUATION = "rag_evaluation", "RAG quality evaluation"
        RAG_SECURITY = "rag_security", "RAG security evaluation"
        CHAOS_EXPERIMENT = "chaos_experiment", "Chaos experiment"
        CAPACITY_AUDIT = "capacity_audit", "Capacity audit"
        RELEASE_GATE = "release_gate", "Release gate"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        PASSED = "passed", "Passed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=32, choices=Kind.choices, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    environment = models.CharField(max_length=40, blank=True)
    git_sha = models.CharField(max_length=64, blank=True)
    parameters = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    failures = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-started_at",)
        indexes = [models.Index(fields=("kind", "status", "-started_at"), name="ops_run_kind_idx")]

    def __str__(self) -> str:
        return f"{self.kind} {self.status} @ {self.started_at:%Y-%m-%d %H:%M}"
