"""Celery entry points for durable agent runs."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import sentry_sdk
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.agents.models import AgentRun


@shared_task(bind=True, acks_late=True, reject_on_worker_lost=True)
def execute_agent_run(self, run_id: str) -> dict:
    """Run or resume one agent run; duplicate deliveries are no-ops once terminal."""
    from apps.agents.engine import execute_run

    run = execute_run(run_id, task_id=getattr(self.request, "id", "") or "")
    return {"status": run.status, "errorCode": run.error_code}


def dispatch_run(run: AgentRun) -> None:
    """Publish a run to its worker queue after the creating transaction commits."""
    task_id = str(uuid4())
    AgentRun.objects.filter(id=run.id).update(celery_task_id=task_id)

    def publish() -> None:
        try:
            execute_agent_run.apply_async(args=[str(run.id)], task_id=task_id, queue="jobs.analysis")
        except Exception as exc:  # noqa: BLE001 - recovery republishes queued runs
            sentry_sdk.capture_exception(exc)

    transaction.on_commit(publish)


@shared_task
def recover_stalled_agent_runs() -> int:
    """Resume runs whose worker died (stale heartbeat) or were never published.

    Resumption continues from the last checkpoint. Runs exceeding
    ``AGENT_RUN_MAX_ATTEMPTS`` fail instead of looping forever.
    """
    from apps.agents.context import AgentError
    from apps.agents.engine import _finish

    cutoff = timezone.now() - timedelta(seconds=settings.AGENT_RUN_STALLED_TIMEOUT_SECONDS)
    from django.db.models import Q

    # Running runs prove liveness through heartbeat_at (written on every step).
    candidates = (
        AgentRun.objects.filter(cancel_requested_at__isnull=True)
        .filter(
            Q(status=AgentRun.Status.RUNNING, heartbeat_at__lt=cutoff)
            | Q(status=AgentRun.Status.QUEUED, updated_at__lt=cutoff)
        )
        .values_list("id", flat=True)[:200]
    )
    recovered = 0
    for run_id in list(candidates):
        run = AgentRun.objects.get(id=run_id)
        if run.attempts >= settings.AGENT_RUN_MAX_ATTEMPTS:
            _finish(
                run, AgentRun.Status.FAILED, error=AgentError("Run stalled repeatedly and was abandoned.")
            )
            continue
        AgentRun.objects.filter(id=run_id).update(status=AgentRun.Status.QUEUED, updated_at=timezone.now())
        try:
            execute_agent_run.apply_async(args=[str(run_id)], queue="jobs.analysis")
        except Exception as exc:  # noqa: BLE001 - next beat tick retries
            sentry_sdk.capture_exception(exc)
            continue
        recovered += 1
    return recovered
