"""Durable agent execution: create, run/resume, finalize, evaluate.

A run is executed by a worker (``apps.agents.tasks.execute_agent_run``). The
graph is compiled with the Django checkpointer keyed by the run id, so a worker
crash resumes from the last checkpoint instead of re-running completed steps.
Every exit path lands in a terminal status with an error code, usage totals,
an evaluation record and an outbox event.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import sentry_sdk
from django.db import transaction
from django.utils import timezone
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError
from langgraph.graph import MessagesState

from apps.agents import safety
from apps.agents.checkpoint import DjangoCheckpointSaver
from apps.agents.context import (
    AgentAwaitingApproval,
    AgentCancelled,
    AgentError,
    AgentSafetyBlocked,
    AgentStepLimitExceeded,
    Budget,
    ExecutionContext,
    PersistentRecorder,
    RunUsage,
)
from apps.agents.graphs import GRAPHS, governed_tool_executor, initial_messages, select_tools
from apps.agents.models import AgentDefinition, AgentEvaluation, AgentRun
from apps.ai_gateway.adapters import AIGatewayError
from apps.events.outbox import add_outbox_event

logger = logging.getLogger(__name__)
EVALUATOR = "heuristic-v1"


def create_run(
    *,
    user: Any,
    organization: Any,
    input_text: str,
    agent: AgentDefinition | None = None,
    graph: str | None = None,
    requested_tools: list[str] | None = None,
    idempotency_key: str = "",
    trace_id: str = "",
    job: Any = None,
) -> tuple[AgentRun, bool]:
    """Persist a queued run (routing happens here, so the chosen graph is auditable).

    Returns ``(run, created)``; an existing run is returned for a replayed idempotency key.
    """
    from apps.agents.router import route

    if idempotency_key:
        existing = AgentRun.objects.filter(
            organization=organization, user=user, idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            return existing, False
    intent = route(
        input_text,
        pinned_graph=(agent.graph if agent else None) or graph,
        organization_id=organization.id,
        trace_id=trace_id,
    )
    spec = GRAPHS[intent.graph]
    budget = Budget.from_settings(
        max_steps=agent.max_steps if agent else None,
        max_model_calls=agent.max_model_calls if agent else None,
        max_tool_calls=agent.max_tool_calls if agent else None,
        max_cost_usd=agent.max_cost_usd if agent else None,
        max_duration_seconds=agent.max_duration_seconds if agent else None,
    )
    tools = select_tools(
        graph=spec.name,
        agent_tools=agent.allowed_tools if agent else None,
        requested=requested_tools,
        organization_id=organization.id,
        user=user,
    )
    run = AgentRun.objects.create(
        organization=organization,
        user=user,
        agent=agent,
        job=job,
        graph=spec.name,
        intent=intent.graph,
        intent_source=intent.source,
        input_text=input_text,
        model_alias=(agent.model_alias if agent and agent.model_alias else spec.model_alias),
        tools=list(tools),
        budget=budget.as_dict(),
        idempotency_key=idempotency_key,
        trace_id=trace_id,
    )
    return run, True


def _budget(run: AgentRun) -> Budget:
    data = run.budget or {}
    return Budget(
        max_steps=int(data.get("maxSteps", 12)),
        max_model_calls=int(data.get("maxModelCalls", 6)),
        max_tool_calls=int(data.get("maxToolCalls", 10)),
        max_cost_usd=Decimal(str(data.get("maxCostUsd", "0.50"))),
        max_duration_seconds=int(data.get("maxDurationSeconds", 300)),
    )


def build_context(run: AgentRun) -> ExecutionContext:
    spec = GRAPHS[run.graph]
    return ExecutionContext(
        user=run.user,
        organization_id=run.organization_id,
        graph=run.graph,
        intent=run.intent,
        budget=_budget(run),
        recorder=PersistentRecorder(run),
        tools=tuple(run.tools or ()),
        task_type=spec.task_type,
        model_alias=run.model_alias or spec.model_alias,
        system_prompt=(run.agent.system_prompt if run.agent and run.agent.system_prompt else None),
        request_id=str(run.request_id),
        trace_id=run.trace_id,
        job_id=str(run.job_id) if run.job_id else None,
        run_id=str(run.id),
        run=run,
        tainted=run.tainted,
        prior=RunUsage(
            model_calls=run.model_calls,
            tool_calls=run.tool_calls,
            cost_usd=Decimal(run.cost_usd),
        ),
        sequence_offset=run.trace.count(),
    )


def _claim(run_id: Any, task_id: str) -> AgentRun | None:
    with transaction.atomic():
        run = AgentRun.objects.select_for_update(of=("self",)).select_related("agent", "user").get(id=run_id)
        if run.is_terminal or run.status == AgentRun.Status.WAITING_APPROVAL:
            return None
        if run.cancel_requested_at is not None:
            _finish(run, AgentRun.Status.CANCELLED, error=AgentCancelled("Cancelled before start."))
            return None
        first_start = run.started_at is None
        run.status = AgentRun.Status.RUNNING
        run.started_at = run.started_at or timezone.now()
        run.heartbeat_at = timezone.now()
        run.attempts += 1
        if task_id:
            run.celery_task_id = task_id
        run.save(
            update_fields=["status", "started_at", "heartbeat_at", "attempts", "celery_task_id", "updated_at"]
        )
        if first_start:
            _event(run, "agents.run.started")
    return run


def _event(run: AgentRun, event_type: str) -> None:
    add_outbox_event(
        event_type,
        str(run.id),
        {
            "runId": str(run.id),
            "organizationId": str(run.organization_id),
            "userId": str(run.user_id),
            "graph": run.graph,
            "status": run.status,
            "errorCode": run.error_code,
            "modelCalls": run.model_calls,
            "toolCalls": run.tool_calls,
            "costUsd": str(run.cost_usd),
        },
        headers={"trace_id": run.trace_id, "request_id": str(run.request_id)},
    )


def _apply_usage(run: AgentRun, ctx: ExecutionContext) -> None:
    run.steps = ctx.sequence_offset + ctx.usage.steps
    run.model_calls += ctx.usage.model_calls
    run.tool_calls += ctx.usage.tool_calls
    run.input_tokens += ctx.usage.input_tokens
    run.output_tokens += ctx.usage.output_tokens
    run.cost_usd += Decimal(ctx.usage.cost_usd)


def _finish(
    run: AgentRun,
    status: str,
    *,
    ctx: ExecutionContext | None = None,
    output: str = "",
    error: AIGatewayError | None = None,
) -> AgentRun:
    """Record a terminal state, evaluation and event exactly once."""
    with transaction.atomic():
        locked = AgentRun.objects.select_for_update().get(id=run.id)
        if locked.is_terminal:
            return locked
        if ctx is not None:
            _apply_usage(locked, ctx)
        locked.status = status
        locked.final_output = output
        locked.error_code = error.code if error else ""
        locked.error_message = str(error)[:2000] if error else ""
        locked.completed_at = timezone.now()
        locked.save()
        _evaluate(locked, ctx)
        _event(locked, f"agents.run.{status}")
    DjangoCheckpointSaver().delete_thread(str(run.id))
    return locked


def _evaluate(run: AgentRun, ctx: ExecutionContext | None) -> None:
    """Heuristic evaluation recorded for every terminal run (regression tracking)."""
    budget = _budget(run)
    invoked = ctx.invoked_tools if ctx else []
    flags = ctx.safety_flags if ctx else []
    metrics = {
        "terminatedNormally": run.status == AgentRun.Status.COMPLETED,
        "answered": bool(run.final_output.strip()),
        "withinBudget": run.model_calls <= budget.max_model_calls
        and run.tool_calls <= budget.max_tool_calls
        and run.cost_usd <= budget.max_cost_usd,
        "groundedWhenRetrieving": ("knowledge.search" not in invoked) or bool(run.final_output.strip()),
        "safetyFlags": len(flags),
        "errorCode": run.error_code,
        "steps": run.steps,
    }
    checks = ["terminatedNormally", "answered", "withinBudget", "groundedWhenRetrieving"]
    score = sum(1.0 for name in checks if metrics[name]) / len(checks)
    AgentEvaluation.objects.update_or_create(
        run=run,
        defaults={
            "evaluator": EVALUATOR,
            "passed": score == 1.0 and not flags,
            "score": score,
            "metrics": metrics,
        },
    )


def _input_gate(run: AgentRun, ctx: ExecutionContext) -> None:
    findings = safety.scan(run.input_text)
    blocked = safety.blocking(findings)
    ctx.step(
        "input_gate",
        "gate",
        outcome="blocked" if blocked else "ok",
        summary="input blocked" if blocked else ("input flagged" if findings else "input accepted"),
        detail={
            "rules": [finding.rule for finding in findings],
            "intent": run.intent,
            "via": run.intent_source,
        },
    )
    if findings:
        ctx.safety_flags.append({"source": "input", "rules": [f.rule for f in findings]})
        safety.record_safety_event(
            organization_id=run.organization_id,
            user=run.user,
            findings=findings,
            blocked=bool(blocked),
            text=run.input_text,
            source="user input",
            trace_id=run.trace_id,
            request_id=str(run.request_id),
        )
    if blocked:
        raise AgentSafetyBlocked("The request was blocked by the safety policy.")


def _final_answer(values: dict[str, Any]) -> str:
    for message in reversed(values.get("messages", [])):
        if isinstance(message, AIMessage) and not message.tool_calls:
            return str(message.content or "")
    return ""


def execute_run(run_id: Any, *, task_id: str = "", executor: Any = None) -> AgentRun:
    """Run or resume ``run_id`` to a terminal state (or an approval pause)."""
    run = _claim(run_id, task_id)
    if run is None:
        return AgentRun.objects.get(id=run_id)
    ctx = build_context(run)
    saver = DjangoCheckpointSaver()
    config: RunnableConfig = {
        "configurable": {"thread_id": str(run.id)},
        "recursion_limit": ctx.budget.max_steps * 2 + 4,
    }
    try:
        resuming = saver.get_tuple(config) is not None
        if not resuming:
            _input_gate(run, ctx)
        graph = GRAPHS[run.graph].builder(ctx, executor or governed_tool_executor).compile(checkpointer=saver)
        graph_input: MessagesState | None = (
            None if resuming else {"messages": initial_messages(run.input_text)}
        )
        graph.invoke(graph_input, config)
        state = graph.get_state(config)
        if state.next:
            # Paused by an interrupt (e.g. a human approval); persist usage so far.
            return _pause(run, ctx)
        return _finish(run, AgentRun.Status.COMPLETED, ctx=ctx, output=_final_answer(state.values))
    except AgentAwaitingApproval:
        # Checkpointed before the tool node; approval resumes the run from there.
        return _pause(run, ctx)
    except AgentCancelled as exc:
        return _finish(run, AgentRun.Status.CANCELLED, ctx=ctx, error=exc)
    except GraphRecursionError:
        return _finish(
            run,
            AgentRun.Status.FAILED,
            ctx=ctx,
            error=AgentStepLimitExceeded("Graph recursion limit reached."),
        )
    except AIGatewayError as exc:
        return _finish(run, AgentRun.Status.FAILED, ctx=ctx, error=exc)
    except Exception as exc:  # noqa: BLE001 - every run must reach a terminal state
        sentry_sdk.capture_exception(exc)
        logger.exception("agent run crashed", extra={"workflow_id": str(run.id)})
        return _finish(run, AgentRun.Status.FAILED, ctx=ctx, error=AgentError(str(exc)[:500]))


def _pause(run: AgentRun, ctx: ExecutionContext) -> AgentRun:
    with transaction.atomic():
        locked = AgentRun.objects.select_for_update().get(id=run.id)
        if locked.is_terminal:
            return locked
        _apply_usage(locked, ctx)
        locked.status = AgentRun.Status.WAITING_APPROVAL
        locked.save()
    return locked


def request_cancel(run: AgentRun) -> AgentRun:
    """Mark cancellation; a queued/paused run ends now, a running one at its next node."""
    with transaction.atomic():
        locked = AgentRun.objects.select_for_update().get(id=run.id)
        if locked.is_terminal:
            return locked
        locked.cancel_requested_at = locked.cancel_requested_at or timezone.now()
        locked.save(update_fields=["cancel_requested_at", "updated_at"])
        if locked.status in {AgentRun.Status.QUEUED, AgentRun.Status.WAITING_APPROVAL}:
            from apps.tools.models import ToolApproval

            ToolApproval.objects.filter(agent_run=locked, status=ToolApproval.Status.PENDING).update(
                status=ToolApproval.Status.EXPIRED, decision_note="Run cancelled."
            )
            return _finish(locked, AgentRun.Status.CANCELLED, error=AgentCancelled("Cancelled by user."))
    return locked
