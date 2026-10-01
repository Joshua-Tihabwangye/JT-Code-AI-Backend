"""Agent execution context: budgets, bounded execution, cancellation and step tracing."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from django.conf import settings
from django.utils import timezone

from apps.ai_gateway.adapters import AIGatewayError


class AgentError(AIGatewayError):
    code = "AGENT_ERROR"


class AgentRuntimeError(AgentError):
    code = "AGENT_RUNTIME_ERROR"


class AgentBudgetExceeded(AgentError):
    code = "AGENT_BUDGET_EXCEEDED"


class AgentMaxIterationsError(AgentBudgetExceeded):
    code = "AGENT_MAX_ITERATIONS_EXCEEDED"


class AgentStepLimitExceeded(AgentBudgetExceeded):
    code = "AGENT_STEP_LIMIT_EXCEEDED"


class AgentDeadlineExceeded(AgentBudgetExceeded):
    code = "AGENT_DEADLINE_EXCEEDED"


class AgentCancelled(AgentError):
    code = "AGENT_CANCELLED"


class AgentSafetyBlocked(AgentError):
    code = "AGENT_SAFETY_BLOCKED"


class AgentAwaitingApproval(AgentError):
    """A side-effecting tool call needs a human decision; the run pauses durably."""

    code = "AGENT_AWAITING_APPROVAL"

    def __init__(self, approval_id: str):
        super().__init__(f"Waiting for approval {approval_id}.")
        self.approval_id = approval_id


@dataclass(frozen=True)
class Budget:
    """Hard limits for one run; every costly action is gated against them."""

    max_steps: int = 12
    max_model_calls: int = 6
    max_tool_calls: int = 10
    max_cost_usd: Decimal = Decimal("0.50")
    max_duration_seconds: int = 300
    max_output_tokens: int | None = None

    @classmethod
    def from_settings(cls, **overrides: Any) -> Budget:
        """Build a budget, clamping any override to the platform-wide ceiling."""
        ceilings = {
            "max_steps": settings.LANGGRAPH_MAX_STEPS,
            "max_model_calls": settings.AGENT_MAX_ITERATIONS,
            "max_tool_calls": settings.AGENT_MAX_TOOL_CALLS,
            "max_cost_usd": Decimal(str(settings.AGENT_MAX_COST_USD)),
            "max_duration_seconds": settings.AGENT_MAX_DURATION_SECONDS,
        }
        values: dict[str, Any] = dict(ceilings)
        for name, value in overrides.items():
            if value is None:
                continue
            ceiling = ceilings.get(name)
            if name == "max_cost_usd":
                value = Decimal(str(value))
            values[name] = min(value, ceiling) if ceiling is not None else value
        return cls(**values)

    def as_dict(self) -> dict[str, Any]:
        return {
            "maxSteps": self.max_steps,
            "maxModelCalls": self.max_model_calls,
            "maxToolCalls": self.max_tool_calls,
            "maxCostUsd": str(self.max_cost_usd),
            "maxDurationSeconds": self.max_duration_seconds,
        }


@dataclass
class RunUsage:
    steps: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")


class Recorder(Protocol):
    def record_step(
        self,
        *,
        node: str,
        kind: str,
        outcome: str,
        summary: str,
        detail: dict[str, Any],
        model_run_id: str | None,
        latency_ms: int | None,
        sequence: int,
    ) -> None: ...

    def is_cancelled(self) -> bool: ...

    def mark_tainted(self) -> None: ...


class MemoryRecorder:
    """Keeps the trace in memory (ephemeral in-process runs and tests)."""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.cancelled = False

    def record_step(self, **step: Any) -> None:
        self.steps.append(step)

    def is_cancelled(self) -> bool:
        return self.cancelled

    def mark_tainted(self) -> None:
        return None


class PersistentRecorder:
    """Writes ``AgentStep`` rows and reads cancellation from the durable run row."""

    def __init__(self, run: Any) -> None:
        self.run = run

    def record_step(
        self,
        *,
        node: str,
        kind: str,
        outcome: str,
        summary: str,
        detail: dict[str, Any],
        model_run_id: str | None,
        latency_ms: int | None,
        sequence: int,
    ) -> None:
        from apps.agents.models import AgentRun, AgentStep

        AgentStep.objects.create(
            run=self.run,
            sequence=sequence,
            node=node,
            kind=kind,
            outcome=outcome,
            summary=summary[:500],
            detail=detail,
            model_run_id=model_run_id if _is_uuid(model_run_id) else None,
            latency_ms=latency_ms,
        )
        AgentRun.objects.filter(id=self.run.id).update(heartbeat_at=timezone.now())

    def is_cancelled(self) -> bool:
        from apps.agents.models import AgentRun

        return AgentRun.objects.filter(id=self.run.id, cancel_requested_at__isnull=False).exists()

    def mark_tainted(self) -> None:
        """Persist taint so a run resumed after an approval pause stays restricted."""
        from apps.agents.models import AgentRun

        AgentRun.objects.filter(id=self.run.id).update(tainted=True)


def _is_uuid(value: str | None) -> bool:
    import uuid

    if not value:
        return False
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


@dataclass
class ExecutionContext:
    """Everything a graph node may use; nodes never reach outside it."""

    user: Any
    organization_id: Any
    graph: str
    budget: Budget
    recorder: Recorder
    tools: tuple[str, ...] = ()
    intent: str = ""
    task_type: str = "GENERAL_QUESTION"
    model_alias: str | None = None
    system_prompt: str | None = None
    temperature: float = 0.7
    request_id: str | None = None
    trace_id: str = ""
    job_id: str | None = None
    run_id: str | None = None
    usage: RunUsage = field(default_factory=RunUsage)
    # Usage recorded by earlier executions of this run (before a crash or approval pause);
    # budgets apply to the whole run, not to each resumption.
    prior: RunUsage = field(default_factory=RunUsage)
    started: float = field(default_factory=time.monotonic)
    # Set when untrusted content (tool output, documents) carried injection indicators.
    tainted: bool = False
    safety_flags: list[dict[str, Any]] = field(default_factory=list)
    invoked_tools: list[str] = field(default_factory=list)
    model_runs: list[str] = field(default_factory=list)
    generate: Callable[..., Any] | None = None
    # The persisted AgentRun (None for ephemeral in-process runs).
    run: Any = None
    # When this execution (first run or resumption) started; earlier tool results are replays.
    resumed_at: datetime = field(default_factory=timezone.now)
    # Sequence offset lets a resumed run keep appending to its trace.
    sequence_offset: int = 0

    # ------------------------------------------------------------------ gates
    def check_live(self) -> None:
        """Stop promptly on cancellation or wall-clock deadline."""
        if self.recorder.is_cancelled():
            raise AgentCancelled("The agent run was cancelled.")
        if time.monotonic() - self.started > self.budget.max_duration_seconds:
            raise AgentDeadlineExceeded(
                f"The agent exceeded its {self.budget.max_duration_seconds}s time budget."
            )

    def before_model_call(self) -> None:
        self.check_live()
        if self.prior.model_calls + self.usage.model_calls >= self.budget.max_model_calls:
            raise AgentMaxIterationsError(
                f"Agent exceeded the {self.budget.max_model_calls}-call iteration limit."
            )
        if self.prior.cost_usd + self.usage.cost_usd >= self.budget.max_cost_usd:
            raise AgentBudgetExceeded(f"Agent reached its ${self.budget.max_cost_usd} cost budget.")

    def is_replay(self, call_id: str) -> bool:
        """True when ``call_id`` already succeeded before this execution resumed."""
        if self.run is None or not call_id:
            return False
        from apps.tools.models import ToolInvocation

        return ToolInvocation.objects.filter(
            agent_run_id=self.run.id,
            tool_call_id=call_id,
            status=ToolInvocation.Status.SUCCEEDED,
            created_at__lt=self.resumed_at,
        ).exists()

    def taint(self, source: str, rules: list[str]) -> None:
        self.tainted = True
        self.safety_flags.append({"source": source, "rules": rules})
        self.recorder.mark_tainted()

    def before_tool_call(self) -> None:
        self.check_live()
        if self.prior.tool_calls + self.usage.tool_calls >= self.budget.max_tool_calls:
            raise AgentBudgetExceeded(f"Agent exceeded the {self.budget.max_tool_calls} tool-call limit.")

    # ---------------------------------------------------------------- tracing
    def step(
        self,
        node: str,
        kind: str,
        *,
        outcome: str = "ok",
        summary: str = "",
        detail: dict[str, Any] | None = None,
        model_run_id: str | None = None,
        latency_ms: int | None = None,
    ) -> None:
        """Record a traced step, enforcing the graph step limit."""
        self.usage.steps += 1
        self.recorder.record_step(
            node=node,
            kind=kind,
            outcome=outcome,
            summary=summary,
            detail=detail or {},
            model_run_id=model_run_id,
            latency_ms=latency_ms,
            sequence=self.sequence_offset + self.usage.steps,
        )
        if self.sequence_offset + self.usage.steps > self.budget.max_steps:
            raise AgentStepLimitExceeded(f"Agent exceeded the {self.budget.max_steps}-step limit.")
