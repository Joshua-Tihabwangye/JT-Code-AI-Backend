"""Persistent agent definitions, runs, traces, evaluations and LangGraph checkpoints."""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class AgentDefinition(models.Model):
    """A tenant-configured agent: graph, tools, model alias and hard budgets."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="agent_definitions"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="agent_definitions", null=True
    )
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120)
    description = models.TextField(blank=True)
    graph = models.CharField(max_length=50)
    system_prompt = models.TextField(blank=True)
    model_alias = models.SlugField(max_length=64, blank=True)
    allowed_tools = models.JSONField(default=list, blank=True)
    max_steps = models.PositiveIntegerField(default=12)
    max_model_calls = models.PositiveIntegerField(default=6)
    max_tool_calls = models.PositiveIntegerField(default=10)
    max_cost_usd = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0.5000"))
    max_duration_seconds = models.PositiveIntegerField(default=300)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)
        constraints = [models.UniqueConstraint(fields=("organization", "slug"), name="uniq_agent_org_slug")]

    def __str__(self) -> str:
        return f"{self.name} ({self.graph})"


class AgentRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        WAITING_APPROVAL = "waiting_approval", "Waiting for approval"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    TERMINAL = frozenset({"completed", "failed", "cancelled"})

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="agent_runs"
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agent_runs")
    agent = models.ForeignKey(
        AgentDefinition, on_delete=models.SET_NULL, related_name="runs", null=True, blank=True
    )
    job = models.ForeignKey(
        "jobs.Job", on_delete=models.SET_NULL, related_name="agent_runs", null=True, blank=True
    )
    graph = models.CharField(max_length=50)
    intent = models.CharField(max_length=50, blank=True)
    intent_source = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    input_text = models.TextField()
    final_output = models.TextField(blank=True)
    model_alias = models.SlugField(max_length=64, blank=True)
    tools = models.JSONField(default=list, blank=True)
    budget = models.JSONField(default=dict, blank=True)
    # Set once untrusted content with injection indicators entered the run; blocks side effects.
    tainted = models.BooleanField(default=False)
    steps = models.PositiveIntegerField(default=0)
    model_calls = models.PositiveIntegerField(default=0)
    tool_calls = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=12, decimal_places=8, default=Decimal("0"))
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    idempotency_key = models.CharField(max_length=255, blank=True)
    request_id = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    trace_id = models.CharField(max_length=100, blank=True, db_index=True)
    celery_task_id = models.CharField(max_length=255, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("organization", "-created_at")),
            models.Index(fields=("status", "heartbeat_at")),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "user", "idempotency_key"),
                condition=~models.Q(idempotency_key=""),
                name="uniq_agent_run_idempotency",
            )
        ]

    def __str__(self) -> str:
        return f"AgentRun {self.id} ({self.graph}, {self.status})"

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL


class AgentStep(models.Model):
    """One traced node execution. Stores summaries and hashes, never raw secrets."""

    class Kind(models.TextChoices):
        GATE = "gate", "Gate"
        ROUTER = "router", "Router"
        MODEL = "model", "Model call"
        TOOL = "tool", "Tool call"
        SYSTEM = "system", "System"

    class Outcome(models.TextChoices):
        OK = "ok", "OK"
        BLOCKED = "blocked", "Blocked"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="trace")
    sequence = models.PositiveIntegerField()
    node = models.CharField(max_length=50)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    outcome = models.CharField(max_length=10, choices=Outcome.choices, default=Outcome.OK)
    summary = models.CharField(max_length=500, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    model_run = models.ForeignKey(
        "ai_gateway.ModelRun", on_delete=models.SET_NULL, related_name="agent_steps", null=True, blank=True
    )
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("run", "sequence")
        constraints = [models.UniqueConstraint(fields=("run", "sequence"), name="uniq_agent_step_sequence")]

    def __str__(self) -> str:
        return f"{self.run_id}#{self.sequence} {self.node} ({self.outcome})"


class AgentEvaluation(models.Model):
    """Automatic post-run evaluation used for regression tracking and review queues."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.OneToOneField(AgentRun, on_delete=models.CASCADE, related_name="evaluation")
    evaluator = models.CharField(max_length=50)
    passed = models.BooleanField()
    score = models.FloatField()
    metrics = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.evaluator}: {'pass' if self.passed else 'fail'} ({self.score:.2f})"


class AgentCheckpoint(models.Model):
    """LangGraph checkpoint (serialized graph state) for resuming a run.

    ``thread_id`` is the ``AgentRun`` id, so checkpoints inherit the run's tenant.
    Checkpoints exist only while a run can still resume; they are deleted when
    the run reaches a terminal state.
    """

    id = models.BigAutoField(primary_key=True)
    thread_id = models.CharField(max_length=64)
    checkpoint_ns = models.CharField(max_length=255, blank=True, default="")
    checkpoint_id = models.CharField(max_length=64)
    parent_checkpoint_id = models.CharField(max_length=64, blank=True, default="")
    type = models.CharField(max_length=30)
    checkpoint = models.BinaryField()
    metadata_type = models.CharField(max_length=30)
    metadata = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("thread_id", "checkpoint_ns", "checkpoint_id"), name="uniq_agent_checkpoint"
            )
        ]
        indexes = [models.Index(fields=("thread_id", "checkpoint_ns", "-checkpoint_id"))]

    def __str__(self) -> str:
        return f"checkpoint {self.thread_id}/{self.checkpoint_id}"


class AgentCheckpointWrite(models.Model):
    """Pending writes recorded against a checkpoint (LangGraph ``put_writes``)."""

    id = models.BigAutoField(primary_key=True)
    thread_id = models.CharField(max_length=64)
    checkpoint_ns = models.CharField(max_length=255, blank=True, default="")
    checkpoint_id = models.CharField(max_length=64)
    task_id = models.CharField(max_length=64)
    idx = models.IntegerField()
    channel = models.CharField(max_length=255)
    type = models.CharField(max_length=30)
    value = models.BinaryField()
    task_path = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"),
                name="uniq_agent_checkpoint_write",
            )
        ]

    def __str__(self) -> str:
        return f"write {self.thread_id}/{self.checkpoint_id}/{self.task_id}#{self.idx}"
