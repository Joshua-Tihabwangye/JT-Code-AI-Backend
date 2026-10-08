"""Per-tenant concurrent-run limits.

Callers invoke :func:`enforce_concurrency` inside the transaction that creates
the run. The organization row is locked first, so two concurrent submissions
cannot both observe a free slot (the same lock order as agent runs).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.db.models import QuerySet

from apps.usage.exceptions import ConcurrencyLimitExceeded
from apps.usage.services import plan_limit


def _active_jobs(organization: Any) -> QuerySet[Any]:
    from apps.jobs.dispatch import NATIVE_TASK_TYPES
    from apps.jobs.models import Job

    # Externally orchestrated task types stay QUEUED by design; only work this
    # backend executes occupies a slot.
    return Job.objects.filter(
        organization=organization,
        task_type__in=NATIVE_TASK_TYPES,
        status__in=[Job.Status.QUEUED, Job.Status.VALIDATING, Job.Status.RUNNING],
    )


def _active_chat_requests(organization: Any) -> QuerySet[Any]:
    from apps.conversations.models import ChatRequest

    return ChatRequest.objects.filter(
        organization=organization, status__in=[ChatRequest.Status.QUEUED, ChatRequest.Status.RUNNING]
    )


def _active_agent_runs(organization: Any) -> QuerySet[Any]:
    from apps.agents.models import AgentRun

    return AgentRun.objects.filter(
        organization=organization,
        status__in=[AgentRun.Status.QUEUED, AgentRun.Status.RUNNING, AgentRun.Status.WAITING_APPROVAL],
    )


def _active_analysis_runs(organization: Any) -> QuerySet[Any]:
    from apps.analytics.models import AnalysisRun

    return AnalysisRun.objects.filter(
        dataset__organization=organization,
        status__in=[AnalysisRun.Status.QUEUED, AnalysisRun.Status.RUNNING],
    )


KINDS: dict[str, tuple[Callable[[Any], QuerySet[Any]], str, str]] = {
    "jobs": (_active_jobs, "max_concurrent_jobs", "MAX_CONCURRENT_JOBS_PER_TENANT"),
    "chat_requests": (
        _active_chat_requests,
        "max_concurrent_chat_requests",
        "MAX_CONCURRENT_CHAT_REQUESTS_PER_TENANT",
    ),
    "agent_runs": (_active_agent_runs, "max_concurrent_agent_runs", "MAX_CONCURRENT_AGENT_RUNS_PER_TENANT"),
    "analysis_runs": (
        _active_analysis_runs,
        "max_concurrent_analysis_runs",
        "MAX_CONCURRENT_ANALYSIS_RUNS_PER_TENANT",
    ),
}


def concurrency_limit(organization: Any, kind: str) -> int:
    _queryset, plan_key, setting = KINDS[kind]
    return plan_limit(organization, plan_key, int(getattr(settings, setting)))


def active_count(organization: Any, kind: str, *, exclude: Any = None) -> int:
    queryset = KINDS[kind][0](organization)
    if exclude is not None:
        queryset = queryset.exclude(pk=exclude)
    return queryset.count()


def enforce_concurrency(
    organization: Any,
    kind: str,
    *,
    exclude: Any = None,
    error: type[Exception] = ConcurrencyLimitExceeded,
) -> None:
    """Raise when ``kind`` is at its limit; must run inside the creating transaction.

    Pass ``exclude`` when the new run row already exists so it does not count
    against its own slot.
    """
    from apps.identity.models import Organization

    Organization.objects.select_for_update().get(id=organization.id)
    if active_count(organization, kind, exclude=exclude) >= concurrency_limit(organization, kind):
        raise error()
