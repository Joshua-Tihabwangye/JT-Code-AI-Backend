"""Durable outbound job callback creation and payload construction."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.jobs.models import Callback, Job


def callback_payload(job: Job) -> dict:
    """Return the immutable terminal event delivered to an external callback."""
    return {
        "event": f"jobs.job.{job.status}",
        "job_id": str(job.id),
        "request_id": str(job.request_id),
        "task_type": job.task_type,
        "status": job.status,
        "result": job.result,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "trace_id": job.trace_id,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def create_terminal_callback(job: Job) -> Callback | None:
    """Create one callback for a terminal job, atomically with its state change."""
    if not job.callback_url or job.status not in {
        Job.Status.COMPLETED,
        Job.Status.FAILED,
        Job.Status.CANCELLED,
        Job.Status.EXPIRED,
    }:
        return None
    callback, _ = Callback.objects.get_or_create(
        job=job,
        url=job.callback_url,
        defaults={
            "payload": callback_payload(job),
            "max_attempts": settings.WEBHOOK_MAX_RETRIES,
            "next_retry_at": timezone.now(),
            "expires_at": timezone.now() + timedelta(days=7),
        },
    )
    return callback
