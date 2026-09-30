from __future__ import annotations

import random
from datetime import timedelta

import httpx
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.jobs.webhooks import signed_callback_request, validate_callback_url


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
    """Requeue any job left running by a lost worker after a safe grace period."""
    from django.conf import settings

    from apps.jobs.dispatch import queue_for_task_type
    from apps.jobs.models import Job

    cutoff = timezone.now() - timedelta(seconds=settings.JOB_STALLED_TIMEOUT_SECONDS)
    recovered = 0
    stalled_jobs = Job.objects.filter(
        status=Job.Status.RUNNING,
        started_at__lt=cutoff,
        cancel_requested_at__isnull=True,
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
            celery_task_id="",
        )
        if not updated:
            continue
        try:
            result = execute_job_task.apply_async(args=[str(job.id)], queue=queue_name)
        except Exception:  # dispatch_queued_jobs retries durable work on the next beat tick
            continue
        Job.objects.filter(id=job.id, status=Job.Status.QUEUED, celery_task_id="").update(
            celery_task_id=result.id
        )
        recovered += 1
    return recovered


@shared_task
def dispatch_queued_jobs() -> int:
    """Republish jobs whose initial broker publication failed after DB commit."""
    from apps.jobs.dispatch import _dispatch_job, queue_for_task_type
    from apps.jobs.models import Job

    dispatched = 0
    jobs = Job.objects.filter(
        status=Job.Status.QUEUED,
        celery_task_id="",
        cancel_requested_at__isnull=True,
    ).order_by("created_at")[:100]
    for job in jobs:
        queue_name = queue_for_task_type(job.task_type)
        Job.objects.filter(id=job.id, status=Job.Status.QUEUED, celery_task_id="").update(
            queue_name=queue_name
        )
        if _dispatch_job(str(job.id), queue_name):
            dispatched += 1
    return dispatched


@shared_task
def process_callbacks() -> dict[str, int]:
    """Deliver terminal callbacks using durable leases and bounded retries."""
    from apps.jobs.models import Callback

    now = timezone.now()
    Callback.objects.filter(
        status__in=[Callback.Status.PENDING, Callback.Status.DELIVERING],
        expires_at__lte=now,
    ).update(status=Callback.Status.EXPIRED, last_error="Callback delivery window expired.")
    lease_cutoff = now - timedelta(seconds=settings.WEBHOOK_DELIVERY_TIMEOUT_SECONDS * 2)
    Callback.objects.filter(
        status=Callback.Status.DELIVERING,
        delivery_started_at__lt=lease_cutoff,
        expires_at__gt=now,
    ).update(
        status=Callback.Status.PENDING,
        next_retry_at=now + timedelta(seconds=settings.WEBHOOK_RETRY_BASE_DELAY),
        last_error="Callback delivery lease expired; retrying.",
    )
    callback_ids = list(
        Callback.objects.filter(
            status=Callback.Status.PENDING,
            next_retry_at__lte=now,
            expires_at__gt=now,
            attempts__lt=F("max_attempts"),
        )
        .order_by("next_retry_at", "created_at")
        .values_list("id", flat=True)[:100]
    )
    delivered = retried = failed = 0
    for callback_id in callback_ids:
        callback = _claim_callback(callback_id)
        if callback is None:
            continue
        try:
            validate_callback_url(callback.url)
            body, headers = signed_callback_request(callback)
            response = httpx.post(
                callback.url,
                content=body,
                headers=headers,
                timeout=settings.WEBHOOK_DELIVERY_TIMEOUT_SECONDS,
                follow_redirects=False,
            )
            outcome = _mark_callback_response(callback.id, response)
        except ValidationError as exc:
            outcome = _mark_callback_failure(callback.id, error=str(exc), permanent=True)
        except httpx.HTTPError as exc:
            outcome = _mark_callback_failure(callback.id, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - external callback isolation
            outcome = _mark_callback_failure(callback.id, error=str(exc))
        if outcome == Callback.Status.DELIVERED:
            delivered += 1
        elif outcome == Callback.Status.FAILED:
            failed += 1
        else:
            retried += 1
    return {"delivered": delivered, "retried": retried, "failed": failed}


def _claim_callback(callback_id):
    from apps.jobs.models import Callback

    now = timezone.now()
    with transaction.atomic():
        callback = Callback.objects.select_for_update().filter(id=callback_id).first()
        if (
            callback is None
            or callback.status != Callback.Status.PENDING
            or callback.next_retry_at is None
            or callback.next_retry_at > now
            or callback.expires_at <= now
            or callback.attempts >= callback.max_attempts
        ):
            return None
        callback.status = Callback.Status.DELIVERING
        callback.attempts += 1
        callback.last_attempt_at = now
        callback.delivery_started_at = now
        callback.save(
            update_fields=[
                "status",
                "attempts",
                "last_attempt_at",
                "delivery_started_at",
                "updated_at",
            ]
        )
        return callback


def _mark_callback_response(callback_id, response: httpx.Response) -> str:
    if 200 <= response.status_code < 300:
        from apps.jobs.models import Callback

        Callback.objects.filter(id=callback_id, status=Callback.Status.DELIVERING).update(
            status=Callback.Status.DELIVERED,
            response_status=response.status_code,
            response_body=response.text[:4000],
            delivered_at=timezone.now(),
            last_error="",
        )
        return Callback.Status.DELIVERED
    return _mark_callback_failure(
        callback_id,
        error=f"Callback returned HTTP {response.status_code}",
        response_status=response.status_code,
        response_body=response.text,
    )


def _mark_callback_failure(
    callback_id,
    *,
    error: str,
    response_status: int | None = None,
    response_body: str = "",
    permanent: bool = False,
) -> str:
    from apps.jobs.models import Callback

    with transaction.atomic():
        callback = Callback.objects.select_for_update().get(id=callback_id)
        if callback.status != Callback.Status.DELIVERING:
            return callback.status
        now = timezone.now()
        exhausted = permanent or callback.attempts >= callback.max_attempts or callback.expires_at <= now
        callback.status = Callback.Status.FAILED if exhausted else Callback.Status.PENDING
        callback.last_error = error[:2000]
        callback.response_status = response_status
        callback.response_body = response_body[:4000]
        callback.next_retry_at = (
            None
            if exhausted
            else now
            + timedelta(
                seconds=min(
                    settings.WEBHOOK_RETRY_MAX_SECONDS,
                    settings.WEBHOOK_RETRY_BASE_DELAY * (2 ** max(callback.attempts - 1, 0)),
                )
            )
        )
        callback.save(
            update_fields=[
                "status",
                "last_error",
                "response_status",
                "response_body",
                "next_retry_at",
                "updated_at",
            ]
        )
        return callback.status


@shared_task
def check_job_deadlines():
    """Check for expired jobs"""
    from apps.jobs.models import Job
    from apps.jobs.transitions import InvalidJobTransition, apply_status_update

    expired_jobs = Job.objects.filter(
        status__in=[Job.Status.QUEUED, Job.Status.RUNNING, Job.Status.VALIDATING], deadline__lt=timezone.now()
    ).values_list("id", flat=True)

    for job_id in expired_jobs:
        try:
            apply_status_update(
                job_id,
                {"status": Job.Status.EXPIRED, "error_message": "Job deadline exceeded"},
            )
        except InvalidJobTransition:
            continue


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
