from __future__ import annotations

from celery import shared_task
from django.utils import timezone


@shared_task(bind=True, autoretry_for=(TimeoutError,), retry_backoff=True, retry_jitter=True, max_retries=3)
def execute_job_task(self, job_id: str) -> dict:
    """Execute an internally-supported job (AI task types) via the agent executor."""
    from apps.jobs.executor import execute_job
    from apps.jobs.models import Job

    job = Job.objects.select_related('organization').get(id=job_id)
    return execute_job(job)


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
            callback.save(update_fields=['status', 'delivered_at'])
        except Exception as e:
            callback.attempts += 1
            callback.last_error = str(e)
            callback.last_attempt_at = timezone.now()
            # Exponential backoff
            callback.next_retry_at = timezone.now() + timezone.timedelta(minutes=2**callback.attempts)
            callback.save(update_fields=['attempts', 'last_error', 'last_attempt_at', 'next_retry_at'])


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
        job.error_message = 'Job deadline exceeded'
        job.save(update_fields=['status', 'completed_at', 'error_message'])

        enqueue_outbox_event(
            topic='jobs.job.expired',
            event_key=str(job.request_id),
            payload={'job_id': str(job.id), 'request_id': str(job.request_id)},
            headers={'trace_id': job.trace_id},
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

    return f'Deleted {count} old jobs'
