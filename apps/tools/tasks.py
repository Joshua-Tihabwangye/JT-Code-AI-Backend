"""Periodic tool-governance tasks."""

from __future__ import annotations

from celery import shared_task
from django.utils import timezone

from apps.tools.models import ToolApproval


@shared_task
def expire_tool_approvals() -> int:
    """Expire stale approvals and resume their runs (the model is told nothing ran)."""
    from apps.tools.approvals import resume_run

    stale = list(
        ToolApproval.objects.filter(
            status=ToolApproval.Status.PENDING, expires_at__lte=timezone.now()
        ).values_list("id", "agent_run_id")[:500]
    )
    expired = ToolApproval.objects.filter(
        id__in=[approval_id for approval_id, _run in stale], status=ToolApproval.Status.PENDING
    ).update(status=ToolApproval.Status.EXPIRED)
    for run_id in {run for _approval, run in stale if run}:
        resume_run(run_id)
    return expired
