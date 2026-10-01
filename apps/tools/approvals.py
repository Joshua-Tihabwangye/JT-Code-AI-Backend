"""Human approval decisions for side-effecting tool calls."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.tools.gateway import ToolDenied, ToolResult, execute_tool
from apps.tools.models import ToolApproval


def _can_decide(user: Any, organization_id: Any) -> bool:
    from apps.identity.authorization import user_can_edit_organization

    return bool(user.organizations.filter(id=organization_id).exists()) and user_can_edit_organization(
        user, organization_id
    )


def resume_run(agent_run_id: Any) -> None:
    """Re-queue a run paused for approval; it resumes from its checkpoint."""
    from apps.agents.models import AgentRun
    from apps.agents.tasks import dispatch_run

    run = AgentRun.objects.filter(id=agent_run_id, status=AgentRun.Status.WAITING_APPROVAL).first()
    if run is None:
        return
    AgentRun.objects.filter(id=run.id, status=AgentRun.Status.WAITING_APPROVAL).update(
        status=AgentRun.Status.QUEUED, updated_at=timezone.now()
    )
    dispatch_run(run)


def decide(
    approval: ToolApproval, *, user: Any, approve: bool, note: str = ""
) -> tuple[ToolApproval, ToolResult | None]:
    """Record a decision; execute direct API calls or resume the paused agent run."""
    if not _can_decide(user, approval.organization_id):
        raise ToolDenied("FORBIDDEN_ROLE", "Editor or admin access is required to decide tool approvals.")
    with transaction.atomic():
        locked = ToolApproval.objects.select_for_update().get(id=approval.id)
        if locked.status != ToolApproval.Status.PENDING:
            raise ToolDenied("APPROVAL_NOT_PENDING", f"This approval is already {locked.status}.")
        if locked.expires_at <= timezone.now():
            locked.status = ToolApproval.Status.EXPIRED
            locked.save(update_fields=["status"])
            raise ToolDenied("APPROVAL_EXPIRED", "This approval has expired.")
        locked.status = ToolApproval.Status.APPROVED if approve else ToolApproval.Status.REJECTED
        locked.decided_by = user
        locked.decided_at = timezone.now()
        locked.decision_note = note[:2000]
        locked.save(update_fields=["status", "decided_by", "decided_at", "decision_note"])
        if locked.agent_run_id:
            run_id = locked.agent_run_id
            transaction.on_commit(lambda: resume_run(run_id))
    result = None
    if locked.agent_run_id is None and approve:
        # A direct API request: run exactly the approved call, once.
        result = execute_tool(
            locked.tool_name,
            locked.arguments,
            user=locked.requested_by,
            organization_id=locked.organization_id,
            source="api",
            approval_id=locked.id,
        )
    locked.refresh_from_db()
    return locked, result
