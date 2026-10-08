"""Celery tasks for n8n orchestration (queue ``orchestration``)."""

from __future__ import annotations

from typing import Any

from celery import shared_task
from django.utils import timezone


@shared_task(acks_late=True, reject_on_worker_lost=True)
def dispatch_workflow_run(run_id: str) -> str:
    from apps.orchestration.runs import dispatch_run

    return dispatch_run(run_id)


@shared_task(acks_late=True, reject_on_worker_lost=True)
def deliver_workflow_event(delivery_id: str) -> str:
    from apps.orchestration.deliveries import deliver

    return deliver(delivery_id)


@shared_task
def sweep_workflows() -> dict[str, Any]:
    """Every minute: due retries, silent executions and stuck deliveries."""
    from apps.orchestration.deliveries import sweep_deliveries
    from apps.orchestration.runs import sweep_runs

    now = timezone.now()
    return {"runs": sweep_runs(now), "deliveries": sweep_deliveries(now)}


@shared_task
def run_due_automations() -> int:
    from apps.orchestration.automations import run_due_automations as run

    return run()


@shared_task
def reconcile_n8n_executions(limit: int = 100) -> dict[str, int]:
    """Ask n8n about long-running executions; failed or vanished ones become failed attempts."""
    from datetime import timedelta

    from apps.jobs.models import WorkflowRun
    from apps.orchestration import client
    from apps.orchestration.runs import record_failed_attempt

    counts = {"checked": 0, "failed": 0}
    try:
        admin = client.N8nAdmin()
    except client.N8nNotConfigured:
        return counts
    cutoff = timezone.now() - timedelta(minutes=10)
    runs = WorkflowRun.objects.filter(
        status=WorkflowRun.Status.RUNNING, max_attempts__gt=0, dispatched_at__lt=cutoff
    ).exclude(n8n_execution_id="")[:limit]
    for run in runs:
        counts["checked"] += 1
        try:
            execution = admin.get_execution(run.n8n_execution_id)
        except client.N8nError as exc:
            if exc.status == 404:
                record_failed_attempt(
                    run.id, run.attempt, code="N8N_EXECUTION_MISSING", message="n8n has no such execution."
                )
                counts["failed"] += 1
            continue
        if execution.get("status") in {"error", "crashed", "canceled"} or (
            execution.get("finished") is False and execution.get("stoppedAt")
        ):
            record_failed_attempt(
                run.id,
                run.attempt,
                code="N8N_EXECUTION_FAILED",
                message=f"n8n execution {run.n8n_execution_id} ended with status {execution.get('status')}.",
            )
            counts["failed"] += 1
    return counts


@shared_task
def prune_workflow_callbacks(days: int = 14) -> int:
    from datetime import timedelta

    from apps.orchestration.models import WorkflowCallback

    cutoff = timezone.now() - timedelta(days=days)
    return WorkflowCallback.objects.filter(received_at__lt=cutoff).delete()[0]
