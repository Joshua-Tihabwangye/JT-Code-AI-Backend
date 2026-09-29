from __future__ import annotations

import random
from datetime import timedelta

from celery import shared_task
from django.utils import timezone


def retry_delay_seconds(retry_count: int) -> int:
    """Bounded exponential backoff with jitter for transient worker failures."""
    return min(300, 2 ** max(retry_count, 0)) + random.randint(0, 3)


@shared_task(bind=True, acks_late=True, reject_on_worker_lost=True)
def execute_job_task(self, job_id: str) -> dict:
    """Execute an internally-supported job while persisting retry/cancel state."""
    from apps.jobs.executor import execute_job
    from apps.jobs.models import Job, JobStep

    job = Job.objects.select_related("organization").get(id=job_id)
    if job.status in {Job.Status.COMPLETED, Job.Status.FAILED, Job.Status.CANCELLED, Job.Status.EXPIRED}:
        return {"status": job.status, "task_type": job.task_type, "idempotent": True}
    if job.cancel_requested_at is not None:
        return {"status": "cancelled", "task_type": job.task_type, "idempotent": True}
    task_id = getattr(self.request, "id", "") or ""
    if task_id and job.celery_task_id != task_id:
        job.celery_task_id = task_id
        job.save(update_fields=["celery_task_id", "updated_at"])
    try:
        return execute_job(job)
    except (TimeoutError, ConnectionError) as exc:
        job.refresh_from_db()
        job.retry_count += 1
        job.last_retry_at = timezone.now()
        job.error_code = "WORKER_RETRY"
        job.error_message = str(exc)[:2000]
        if job.retry_count > job.max_retries:
            job.status = Job.Status.FAILED
            job.completed_at = timezone.now()
            job.save(
                update_fields=[
                    "retry_count",
                    "last_retry_at",
                    "error_code",
                    "error_message",
                    "status",
                    "completed_at",
                    "updated_at",
                ]
            )
            JobStep.objects.filter(job=job, status=JobStep.Status.RUNNING).update(
                status=JobStep.Status.FAILED,
                error_message=job.error_message,
                completed_at=job.completed_at,
            )
            return {"status": "failed", "task_type": job.task_type, "error_code": "WORKER_RETRY"}
        job.status = Job.Status.QUEUED
        job.save(
            update_fields=[
                "retry_count",
                "last_retry_at",
                "error_code",
                "error_message",
                "status",
                "updated_at",
            ]
        )
        raise self.retry(
            exc=exc, countdown=retry_delay_seconds(job.retry_count), max_retries=job.max_retries
        ) from exc


@shared_task
def recover_stalled_jobs() -> int:
    """Requeue native jobs left running by a lost worker after a safe grace period."""
    from django.conf import settings

    from apps.jobs.dispatch import NATIVE_TASK_TYPES, queue_for_task_type
    from apps.jobs.models import Job

    cutoff = timezone.now() - timedelta(seconds=settings.JOB_STALLED_TIMEOUT_SECONDS)
    recovered = 0
    stalled_jobs = Job.objects.filter(
        status=Job.Status.RUNNING,
        started_at__lt=cutoff,
        cancel_requested_at__isnull=True,
        task_type__in=NATIVE_TASK_TYPES,
    ).iterator()
    for job in stalled_jobs:
        queue_name = queue_for_task_type(job.task_type)
        updated = Job.objects.filter(
            id=job.id,
            status=Job.Status.RUNNING,
            started_at__lt=cutoff,
            cancel_requested_at__isnull=True,
        ).update(
            status=Job.Status.QUEUED,
            queue_name=queue_name,
            error_code="WORKER_RECOVERY",
            error_message="Recovered after worker heartbeat timeout.",
        )
        if not updated:
            continue
        result = execute_job_task.apply_async(args=[str(job.id)], queue=queue_name)
        Job.objects.filter(id=job.id, status=Job.Status.QUEUED).update(celery_task_id=result.id)
        recovered += 1
    return recovered


@shared_task
def process_callbacks():
    """Process pending callbacks"""
    from apps.jobs.models import Callback

    callbacks = Callback.objects.filter(
        status=Callback.Status.PENDING, next_retry_at__lte=timezone.now(), attempts__lt=5
    )[:100]

    for callback in callbacks:
        try:
            # This would make HTTP request to callback.url
            # For now, just mark as delivered
            callback.status = Callback.Status.DELIVERED
            callback.delivered_at = timezone.now()
            callback.save(update_fields=["status", "delivered_at"])
        except Exception as e:
            callback.attempts += 1
            callback.last_error = str(e)
            callback.last_attempt_at = timezone.now()
            # Exponential backoff
            callback.next_retry_at = timezone.now() + timezone.timedelta(minutes=2**callback.attempts)
            callback.save(update_fields=["attempts", "last_error", "last_attempt_at", "next_retry_at"])


@shared_task
def check_job_deadlines():
    """Check for expired jobs"""
    from apps.events.outbox import enqueue_outbox_event
    from apps.jobs.models import Job

    expired_jobs = Job.objects.filter(
        status__in=[Job.Status.QUEUED, Job.Status.RUNNING, Job.Status.VALIDATING], deadline__lt=timezone.now()
    )

    for job in expired_jobs:
        job.status = Job.Status.EXPIRED
        job.completed_at = timezone.now()
        job.error_message = "Job deadline exceeded"
        job.save(update_fields=["status", "completed_at", "error_message"])

        enqueue_outbox_event(
            topic="jobs.job.expired",
            event_key=str(job.request_id),
            payload={"job_id": str(job.id), "request_id": str(job.request_id)},
            headers={"trace_id": job.trace_id},
        )


@shared_task
def expire_old_jobs():
    """Clean up very old jobs"""
    from apps.jobs.models import Job

    cutoff = timezone.now() - timezone.timedelta(days=90)
    old_jobs = Job.objects.filter(
        created_at__lt=cutoff,
        status__in=[Job.Status.COMPLETED, Job.Status.FAILED, Job.Status.CANCELLED, Job.Status.EXPIRED],
    )

    count = old_jobs.count()
    old_jobs.delete()

    return f"Deleted {count} old jobs"
