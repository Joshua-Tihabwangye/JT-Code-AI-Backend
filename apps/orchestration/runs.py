"""Job workflows: Django jobs executed by n8n with durable attempts and retries.

Lifecycle of a :class:`~apps.jobs.models.WorkflowRun` for an n8n task type::

    PENDING --dispatch (attempt n)--> RUNNING --callback completed--> COMPLETED
       ^                                 |
       +---- retry after backoff <-------+-- dispatch error / callback failed / timeout
                                         +-- attempts exhausted --> FAILED (job failed, hold released)

* Every dispatch increments ``attempt``; callbacks must name the current
  attempt, so a late callback from an abandoned execution cannot change state.
* A dispatch holds a short lease (``next_attempt_at``) so the sweeper never
  sends the same attempt twice while a request is in flight.
* Job status, results and credits are applied only through
  :func:`apps.jobs.transitions.apply_status_update` (canonical state).
"""

from __future__ import annotations

import contextlib
import logging
import random
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.metrics import WORKFLOW_DISPATCHES
from apps.core.tracing import inject_headers
from apps.events.outbox import enqueue_outbox_event
from apps.jobs.models import Job, WorkflowRun
from apps.jobs.transitions import TERMINAL_STATUSES, InvalidJobTransition, apply_status_update
from apps.orchestration import client
from apps.orchestration.registry import definition_for_task

logger = logging.getLogger(__name__)
ORCHESTRATION_QUEUE = "orchestration"


def backoff_seconds(attempt: int) -> int:
    base = settings.N8N_RETRY_BASE_SECONDS * 2 ** max(0, attempt - 1)
    delay = min(settings.N8N_RETRY_MAX_SECONDS, base)
    return int(delay + random.uniform(0, delay * 0.1))  # nosec B311 - retry jitter


def _event(run: WorkflowRun, name: str, **data: Any) -> None:
    enqueue_outbox_event(
        topic=f"orchestration.workflow.{name}",
        event_key=str(run.job_id),
        payload={
            "run_id": str(run.id),
            "job_id": str(run.job_id),
            "workflow": run.workflow_key,
            "version": run.workflow_version,
            "attempt": run.attempt,
            "organization_id": str(run.job.organization_id),
            **data,
        },
        headers={"trace_id": run.job.trace_id},
    )


def schedule_dispatch(run_id: Any) -> None:
    from apps.orchestration.tasks import dispatch_workflow_run

    transaction.on_commit(lambda: _safe_apply(dispatch_workflow_run, str(run_id)))


def _safe_apply(task: Any, *args: str) -> None:
    try:
        task.apply_async(args=list(args), queue=ORCHESTRATION_QUEUE)
    except Exception:  # noqa: BLE001 - the sweeper re-dispatches due runs from the database
        logger.warning("could not publish orchestration task; the sweeper will retry", exc_info=True)


def start_job_workflow(job: Job) -> WorkflowRun:
    """Bind ``job`` to its n8n workflow and schedule the first attempt (caller's transaction)."""
    definition = definition_for_task(job.task_type)
    if definition is None:
        raise InvalidJobTransition(f"No active n8n workflow handles {job.task_type}.")
    run, _ = WorkflowRun.objects.update_or_create(
        job=job,
        defaults={
            "n8n_workflow_id": definition.n8n_workflow_id or definition.key,
            "workflow_key": definition.key,
            "workflow_version": definition.version,
            "max_attempts": definition.max_attempts,
            "timeout_seconds": definition.timeout_seconds,
            "input_payload": job.input_payload,
            "status": WorkflowRun.Status.PENDING,
            "next_attempt_at": timezone.now(),
        },
    )
    schedule_dispatch(run.id)
    return run


def _fail_job(job_id: Any, code: str, message: str) -> None:
    with contextlib.suppress(InvalidJobTransition):
        apply_status_update(
            job_id, {"status": Job.Status.FAILED, "error_code": code, "error_message": message}
        )


def build_job_payload(run: WorkflowRun) -> dict[str, Any]:
    job = run.job
    base = f"n8n/runs/{run.id}"
    return {
        "kind": "job",
        "runId": str(run.id),
        "jobId": str(job.id),
        "attempt": run.attempt,
        "maxAttempts": run.max_attempts,
        "taskType": job.task_type,
        "organizationId": str(job.organization_id),
        "input": job.input_payload,
        "deadline": job.deadline.isoformat() if job.deadline else None,
        "callbacks": {
            "status": client.callback_url(f"{base}/status/"),
            "events": client.callback_url(f"{base}/events/"),
            "state": client.callback_url(f"{base}/"),
        },
        "traceparent": inject_headers({}).get("traceparent", ""),
        "sentAt": timezone.now().isoformat(),
    }


def dispatch_run(run_id: Any) -> str:
    """Send the next attempt of ``run_id`` to n8n. Returns the outcome."""
    now = timezone.now()
    with transaction.atomic():
        run = (
            WorkflowRun.objects.select_for_update(of=("self",))
            .select_related("job")
            .filter(id=run_id)
            .first()
        )
        if run is None:
            return "missing"
        job = run.job
        if job.status in TERMINAL_STATUSES or job.cancel_requested_at:
            if run.status not in (WorkflowRun.Status.COMPLETED, WorkflowRun.Status.FAILED):
                run.status = WorkflowRun.Status.CANCELLED
                run.next_attempt_at = None
                run.save(update_fields=["status", "next_attempt_at", "updated_at"])
            return "cancelled"
        if run.status != WorkflowRun.Status.PENDING or (run.next_attempt_at and run.next_attempt_at > now):
            return "not_due"
        if not client.configured():
            run.status = WorkflowRun.Status.FAILED
            run.error_message = "n8n is not configured."
            run.save(update_fields=["status", "error_message", "updated_at"])
            transaction.on_commit(lambda: _fail_job(job.id, "N8N_NOT_CONFIGURED", "n8n is not configured."))
            return "not_configured"
        definition = definition_for_task(job.task_type)
        if definition is None:
            transaction.on_commit(
                lambda: _fail_job(job.id, "N8N_WORKFLOW_MISSING", f"No n8n workflow for {job.task_type}.")
            )
            return "no_workflow"
        run.attempt += 1
        run.dispatched_at = now
        # Lease: while the request is in flight the sweeper treats the run as not due.
        run.next_attempt_at = now + timedelta(seconds=settings.N8N_REQUEST_TIMEOUT_SECONDS * 4)
        run.workflow_key, run.workflow_version = definition.key, definition.version
        run.save(
            update_fields=[
                "attempt",
                "dispatched_at",
                "next_attempt_at",
                "workflow_key",
                "workflow_version",
                "updated_at",
            ]
        )
        attempt, webhook_path = run.attempt, definition.webhook_path
        payload = build_job_payload(run)
    try:
        response = client.post_signed(webhook_path, payload, idempotency_key=f"{run.id}:{attempt}")
    except client.N8nError as exc:
        WORKFLOW_DISPATCHES.labels(run.workflow_key, "retryable" if exc.retryable else "rejected").inc()
        record_failed_attempt(
            run.id, attempt, code="N8N_DISPATCH_FAILED", message=str(exc), retryable=exc.retryable
        )
        return "failed"
    WORKFLOW_DISPATCHES.labels(run.workflow_key, "accepted").inc()
    accepted = client.response_json(response)
    with transaction.atomic():
        run = WorkflowRun.objects.select_for_update(of=("self",)).select_related("job").get(id=run.id)
        if run.attempt != attempt or run.status != WorkflowRun.Status.PENDING:
            return "superseded"
        run.status = WorkflowRun.Status.RUNNING
        run.n8n_execution_id = str(accepted.get("executionId") or "")[:100]
        run.started_at = run.started_at or timezone.now()
        run.deadline_at = timezone.now() + timedelta(seconds=run.timeout_seconds)
        run.next_attempt_at = None
        run.save(
            update_fields=[
                "status",
                "n8n_execution_id",
                "started_at",
                "deadline_at",
                "next_attempt_at",
                "updated_at",
            ]
        )
        if run.job.status == Job.Status.QUEUED:
            apply_status_update(
                run.job_id, {"status": Job.Status.RUNNING, "n8n_execution_id": run.n8n_execution_id}
            )
        _event(run, "dispatched", execution_id=run.n8n_execution_id)
    return "accepted"


def record_failed_attempt(
    run_id: Any, attempt: int, *, code: str, message: str, retryable: bool = True
) -> str:
    """Retry with backoff while attempts remain, else fail the job. Ignores stale attempts."""
    with transaction.atomic():
        run = (
            WorkflowRun.objects.select_for_update(of=("self",))
            .select_related("job")
            .filter(id=run_id)
            .first()
        )
        if run is None or run.attempt != attempt:
            return "stale"
        if run.status in (
            WorkflowRun.Status.COMPLETED,
            WorkflowRun.Status.FAILED,
            WorkflowRun.Status.CANCELLED,
        ):
            return "terminal"
        if run.job.status in TERMINAL_STATUSES or run.job.cancel_requested_at:
            return "terminal"
        run.error_message = message[:2000]
        run.last_error_code = code[:64]
        if retryable and run.attempt < run.max_attempts:
            delay = backoff_seconds(run.attempt)
            run.status = WorkflowRun.Status.PENDING
            run.next_attempt_at = timezone.now() + timedelta(seconds=delay)
            run.deadline_at = None
            run.save(
                update_fields=[
                    "status",
                    "next_attempt_at",
                    "deadline_at",
                    "error_message",
                    "last_error_code",
                    "updated_at",
                ]
            )
            _event(run, "retry_scheduled", error_code=code, retry_in_seconds=delay)
            return "retry_scheduled"
        run.save(update_fields=["error_message", "last_error_code", "updated_at"])
        _event(run, "failed", error_code=code, error=message[:500])
        job_id = run.job_id
    _fail_job(job_id, code, message[:2000])
    return "failed"


def apply_run_callback(run_id: Any, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Apply a verified status callback; returns ``(http_status, body)``."""
    status_value = data["status"]
    with transaction.atomic():
        run = (
            WorkflowRun.objects.select_for_update(of=("self",))
            .select_related("job")
            .filter(id=run_id)
            .first()
        )
        if run is None:
            return 404, {"detail": "Workflow run not found."}
        job = run.job
        if data["attempt"] != run.attempt:
            return 409, {"detail": "Callback is for a superseded attempt.", "code": "stale_attempt"}
        if job.status in TERMINAL_STATUSES:
            same = status_value == job.status
            return (200 if same else 409), {"detail": "Job is already terminal.", "jobStatus": job.status}
        if run.status == WorkflowRun.Status.PENDING and status_value != "failed":
            # A callback can arrive before the dispatch response is recorded.
            run.status = WorkflowRun.Status.RUNNING
        run.last_callback_at = timezone.now()
        run.deadline_at = timezone.now() + timedelta(seconds=run.timeout_seconds)  # heartbeat
        if data.get("execution_id"):
            run.n8n_execution_id = data["execution_id"][:100]
        for field in ("progress_percent", "steps_completed", "total_steps"):
            if field in data:
                setattr(run, field, data[field])
        run.save()
        attempt = run.attempt
        if status_value == "running":
            if job.status == Job.Status.QUEUED:
                apply_status_update(job.id, {"status": Job.Status.RUNNING})
            update = {k: data[k] for k in ("progress_percent",) if k in data}
            if update:
                job.progress_percent = update["progress_percent"]
                job.save(update_fields=["progress_percent", "updated_at"])
            _event(run, "progress", progress=data.get("progress_percent"))
            return 200, {"accepted": True, "jobStatus": Job.Status.RUNNING}
        if status_value == "waiting_approval":
            apply_status_update(job.id, {"status": Job.Status.WAITING_APPROVAL})
            _event(run, "progress", waiting_approval=True)
            return 200, {"accepted": True, "jobStatus": Job.Status.WAITING_APPROVAL}
        if status_value == "completed":
            run.status = WorkflowRun.Status.COMPLETED
            run.save(update_fields=["status", "updated_at"])
            update = {
                "status": Job.Status.COMPLETED,
                "result": data.get("result") or {},
                "n8n_execution_id": run.n8n_execution_id,
            }
            if data.get("actual_credits") is not None:
                update["actual_credits"] = data["actual_credits"]
            apply_status_update(job.id, update)
            _event(run, "completed", execution_id=run.n8n_execution_id)
            return 200, {"accepted": True, "jobStatus": Job.Status.COMPLETED}
    error = data.get("error") or {}
    outcome = record_failed_attempt(
        run_id,
        attempt,
        code=str(error.get("code") or "N8N_WORKFLOW_FAILED"),
        message=str(error.get("message") or "The n8n workflow reported a failure."),
        retryable=bool(error.get("retryable", True)),
    )
    return 200, {"accepted": True, "outcome": outcome}


def run_state(run_id: Any) -> dict[str, Any] | None:
    run = WorkflowRun.objects.select_related("job").filter(id=run_id).first()
    if run is None:
        return None
    job = run.job
    return {
        "runId": str(run.id),
        "attempt": run.attempt,
        "status": run.status,
        "jobStatus": job.status,
        "cancelled": job.status == Job.Status.CANCELLED or bool(job.cancel_requested_at),
    }


def sweep_runs(now: Any = None) -> dict[str, int]:
    """Dispatch due attempts and expire attempts whose n8n execution went silent."""
    now = now or timezone.now()
    counts = {"dispatched": 0, "timed_out": 0}
    due = WorkflowRun.objects.filter(
        status=WorkflowRun.Status.PENDING, next_attempt_at__lte=now, max_attempts__gt=0
    ).values_list("id", flat=True)[:200]
    for run_id in due:
        if dispatch_run(run_id) in {"accepted", "failed"}:
            counts["dispatched"] += 1
    silent = WorkflowRun.objects.filter(
        status=WorkflowRun.Status.RUNNING, deadline_at__lt=now, max_attempts__gt=0
    ).values_list("id", "attempt")[:200]
    for run_id, attempt in silent:
        record_failed_attempt(
            run_id,
            attempt,
            code="N8N_TIMEOUT",
            message="The n8n execution sent no callback before its timeout.",
        )
        counts["timed_out"] += 1
    return counts


def fail_by_execution(execution_id: str, message: str) -> str | None:
    """Treat an n8n error-workflow report as a failed attempt of the matching run."""
    run = (
        WorkflowRun.objects.filter(n8n_execution_id=execution_id, status=WorkflowRun.Status.RUNNING)
        .only("id", "attempt")
        .first()
    )
    if run is None:
        return None
    return record_failed_attempt(run.id, run.attempt, code="N8N_EXECUTION_ERROR", message=message)
