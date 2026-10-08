"""Resolve whether a metered source finished and what it actually cost.

Settlement hooks call :func:`apps.usage.services.finalize_source` at terminal
transitions; the ``settle_finished_reservations`` sweep calls the same resolver
for every outstanding hold, so a missed hook can never leak reserved credits.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.db.models import Count, Sum

from apps.usage.services import UsageCost


@dataclass(frozen=True)
class SourceState:
    terminal: bool
    succeeded: bool = False
    cost: UsageCost = field(default_factory=UsageCost)
    quantity: int | None = None
    reason: str = ""


def _model_run_cost(**filters: Any) -> UsageCost:
    from apps.ai_gateway.models import ModelRun

    totals = ModelRun.objects.filter(status=ModelRun.Status.COMPLETED, **filters).aggregate(
        cost=Sum("provider_cost_usd"),
        input=Sum("input_tokens"),
        output=Sum("output_tokens"),
        runs=Count("id"),
    )
    return UsageCost(
        provider_cost_usd=Decimal(totals["cost"] or 0),
        input_tokens=int(totals["input"] or 0),
        output_tokens=int(totals["output"] or 0),
        model_run_count=int(totals["runs"] or 0),
    )


def _job(source_id: str) -> SourceState:
    from apps.jobs.models import Job
    from apps.jobs.transitions import TERMINAL_STATUSES

    job = Job.objects.filter(id=source_id).first()
    if job is None:
        return SourceState(terminal=True, reason="job deleted")
    if job.status not in TERMINAL_STATUSES:
        return SourceState(terminal=False)
    if job.status != Job.Status.COMPLETED:
        return SourceState(terminal=True, reason=f"job {job.status}")
    return SourceState(terminal=True, succeeded=True, cost=_model_run_cost(job_id=job.id))


def _chat_request(source_id: str) -> SourceState:
    from apps.conversations.models import ChatRequest

    request = ChatRequest.objects.filter(id=source_id).first()
    if request is None:
        return SourceState(terminal=True, reason="chat request deleted")
    if request.status == ChatRequest.Status.COMPLETED:
        return SourceState(terminal=True, succeeded=True, cost=_model_run_cost(request_id=request.id))
    if request.status in {ChatRequest.Status.FAILED, ChatRequest.Status.CANCELLED}:
        return SourceState(terminal=True, reason=f"chat request {request.status}")
    return SourceState(terminal=False)


def _agent_run(source_id: str) -> SourceState:
    from apps.agents.models import AgentRun

    run = AgentRun.objects.filter(id=source_id).first()
    if run is None:
        return SourceState(terminal=True, reason="agent run deleted")
    if not run.is_terminal:
        return SourceState(terminal=False)
    cost = UsageCost(
        provider_cost_usd=Decimal(run.cost_usd or 0),
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        model_run_count=run.model_calls,
    )
    # A failed or cancelled run still consumed model calls; charge what it used.
    return SourceState(
        terminal=True, succeeded=run.model_calls > 0 or run.status == AgentRun.Status.COMPLETED, cost=cost
    )


def _conversion(source_id: str) -> SourceState:
    from apps.conversions.models import ConversionJob

    job = ConversionJob.objects.filter(id=source_id).first()
    if job is None:
        return SourceState(terminal=True, reason="conversion deleted")
    status = str(job.status).lower()
    if status in {"completed", "succeeded", "ready"}:
        return SourceState(terminal=True, succeeded=True)
    if status in {"failed", "cancelled", "canceled", "expired"}:
        return SourceState(terminal=True, reason=f"conversion {status}")
    return SourceState(terminal=False)


def _analysis_run(source_id: str) -> SourceState:
    from apps.analytics.models import AnalysisRun

    run = AnalysisRun.objects.filter(id=source_id).first()
    if run is None:
        return SourceState(terminal=True, reason="analysis run deleted")
    if run.status == AnalysisRun.Status.COMPLETED:
        return SourceState(terminal=True, succeeded=True)
    if run.status == AnalysisRun.Status.FAILED:
        return SourceState(terminal=True, reason="analysis failed")
    return SourceState(terminal=False)


def _completed_inline(source_id: str) -> SourceState:
    # Synchronous operations settle inline; a leftover hold means the request
    # crashed before completing, so it is released.
    return SourceState(terminal=True, reason="synchronous request did not settle")


RESOLVERS: dict[str, Callable[[str], SourceState]] = {
    "job": _job,
    "chat_request": _chat_request,
    "agent_run": _agent_run,
    "conversion": _conversion,
    "analysis_run": _analysis_run,
    "image_generation": _completed_inline,
    "document_render": _completed_inline,
    "knowledge_search": _completed_inline,
    "embedding": _completed_inline,
}


def resolve(source_type: str, source_id: Any) -> SourceState | None:
    resolver = RESOLVERS.get(source_type)
    return resolver(str(source_id)) if resolver else None
