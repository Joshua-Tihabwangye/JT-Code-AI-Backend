"""Atomic, idempotent job state transitions and terminal side effects."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.billing.services import CreditService
from apps.events.outbox import enqueue_outbox_event
from apps.jobs.models import Job, WorkflowRun

TERMINAL_STATUSES = frozenset(
    {Job.Status.COMPLETED, Job.Status.FAILED, Job.Status.CANCELLED, Job.Status.EXPIRED}
)
_ALLOWED_TRANSITIONS = {
    Job.Status.QUEUED: {
        Job.Status.VALIDATING,
        Job.Status.RUNNING,
        Job.Status.WAITING_APPROVAL,
        *TERMINAL_STATUSES,
    },
    Job.Status.VALIDATING: {
        Job.Status.QUEUED,
        Job.Status.RUNNING,
        Job.Status.WAITING_APPROVAL,
        *TERMINAL_STATUSES,
    },
    Job.Status.RUNNING: {
        Job.Status.VALIDATING,
        Job.Status.WAITING_APPROVAL,
        *TERMINAL_STATUSES,
    },
    Job.Status.WAITING_APPROVAL: {
        Job.Status.QUEUED,
        Job.Status.RUNNING,
        *TERMINAL_STATUSES,
    },
}


class InvalidJobTransition(ValueError):
    """Raised when an integration attempts a backward or terminal transition."""


def settle_terminal_credits(job: Job, *, actual_credits: Decimal | None = None) -> None:
    """Settle a completed job or release every other terminal reservation once."""
    if job.status == Job.Status.COMPLETED:
        amount = actual_credits if actual_credits is not None else job.reserved_credits
        if job.reserved_credits <= 0:
            return
        if amount > job.reserved_credits:
            raise InvalidJobTransition("actual_credits cannot exceed reserved_credits.")
        job.actual_credits = amount
        job.save(update_fields=["actual_credits", "updated_at"])
        CreditService.settle_reservation(
            user=job.owner,
            request_id=job.request_id,
            actual_amount=amount,
            organization=job.organization,
        )
    elif job.status in TERMINAL_STATUSES:
        CreditService.release_reservation(
            user=job.owner,
            request_id=job.request_id,
            organization=job.organization,
        )


def _update_workflow(job: Job, data: dict, *, terminal: bool) -> None:
    workflow = WorkflowRun.objects.select_for_update().filter(job=job).first()
    if workflow is None:
        return
    changed: list[str] = []
    for field in ("progress_percent", "steps_completed", "total_steps"):
        if field in data:
            setattr(workflow, field, data[field])
            changed.append(field)
    if terminal:
        workflow.status = WorkflowRun.Status.FAILED if job.status == Job.Status.EXPIRED else job.status
        workflow.completed_at = job.completed_at
        changed.extend(("status", "completed_at"))
        if "result" in data:
            workflow.output_payload = data["result"]
            changed.append("output_payload")
        if "error_message" in data:
            workflow.error_message = data["error_message"]
            changed.append("error_message")
    if changed:
        workflow.save(update_fields=[*set(changed), "updated_at"])


def apply_status_update(job_id, data: dict) -> tuple[Job, bool]:
    """Apply an authenticated integration update with exactly-once terminal effects."""
    with transaction.atomic():
        job = Job.objects.select_for_update().select_related("organization", "owner").get(id=job_id)
        old_status, new_status = job.status, data["status"]
        if old_status in TERMINAL_STATUSES:
            if new_status != old_status:
                raise InvalidJobTransition(f"Cannot transition terminal job from {old_status}.")
            return job, False
        if new_status != old_status and new_status not in _ALLOWED_TRANSITIONS.get(old_status, set()):
            raise InvalidJobTransition(f"Cannot transition job from {old_status} to {new_status}.")

        terminal = new_status in TERMINAL_STATUSES
        changed: list[str] = []
        for field in ("result", "error_code", "error_message", "n8n_execution_id", "progress_percent"):
            if field in data:
                setattr(job, field, data[field])
                changed.append(field)
        if new_status != old_status:
            job.status = new_status
            changed.append("status")
        if terminal:
            job.completed_at = timezone.now()
            changed.append("completed_at")
            if new_status == Job.Status.COMPLETED:
                job.progress_percent = 100
                changed.append("progress_percent")
            if new_status == Job.Status.CANCELLED:
                job.cancel_requested_at = timezone.now()
                changed.append("cancel_requested_at")
        if changed:
            job.save(update_fields=[*set(changed), "updated_at"])
        _update_workflow(job, data, terminal=terminal)
        if terminal and new_status != old_status:
            settle_terminal_credits(job, actual_credits=data.get("actual_credits"))
            from apps.jobs.callbacks import create_terminal_callback

            create_terminal_callback(job)
        if new_status != old_status:
            enqueue_outbox_event(
                topic=f"jobs.job.{new_status}",
                event_key=str(job.request_id),
                payload={
                    "job_id": str(job.id),
                    "request_id": str(job.request_id),
                    "old_status": old_status,
                    "new_status": new_status,
                    "result": data.get("result"),
                    "error": data.get("error_message"),
                },
                headers={"trace_id": job.trace_id},
            )
        return job, new_status != old_status
