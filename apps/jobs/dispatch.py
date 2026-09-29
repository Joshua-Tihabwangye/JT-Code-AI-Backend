"""Durable job dispatch and workload-to-queue policy."""

from __future__ import annotations

from django.conf import settings
from django.db import transaction

from apps.events.outbox import enqueue_outbox_event
from apps.jobs.models import Job, WorkflowRun

ANALYSIS_QUEUE = "jobs.analysis"
INGESTION_QUEUE = "jobs.ingestion"
VISUALIZATION_QUEUE = "jobs.visualization"
DEFAULT_QUEUE = "jobs.default"

NATIVE_TASK_TYPES = frozenset(
    {
        Job.TaskType.GENERAL_QUESTION,
        Job.TaskType.RAG_QUERY,
        Job.TaskType.SEARCH_RESEARCH,
    }
)


def queue_for_task_type(task_type: str) -> str:
    if task_type == Job.TaskType.KNOWLEDGE_INGESTION:
        return INGESTION_QUEUE
    if task_type in {
        Job.TaskType.IMAGE_UNDERSTANDING,
        Job.TaskType.IMAGE_GENERATION,
        Job.TaskType.DOCUMENT_DRAFTING,
        Job.TaskType.DOCUMENT_RENDERING,
        Job.TaskType.FILE_CONVERSION,
    }:
        return VISUALIZATION_QUEUE
    if task_type in NATIVE_TASK_TYPES:
        return ANALYSIS_QUEUE
    return DEFAULT_QUEUE


def is_native_job(job: Job) -> bool:
    return job.task_type in NATIVE_TASK_TYPES


def enqueue_job(job: Job) -> None:
    """Persist dispatch intent before making work visible to a worker or integration."""
    queue_name = queue_for_task_type(job.task_type)
    if job.queue_name != queue_name:
        job.queue_name = queue_name
        job.save(update_fields=["queue_name", "updated_at"])
    WorkflowRun.objects.get_or_create(
        job=job,
        defaults={
            "n8n_workflow_id": job.task_type.lower(),
            "input_payload": job.input_payload,
        },
    )
    enqueue_outbox_event(
        topic="jobs.job.created",
        event_key=str(job.request_id),
        payload={
            "job_id": str(job.id),
            "request_id": str(job.request_id),
            "task_type": job.task_type,
            "queue_name": queue_name,
            "input_payload": job.input_payload,
            "owner_id": str(job.owner_id),
            "organization_id": str(job.organization_id),
            "reserved_credits": str(job.reserved_credits),
            "callback_url": job.callback_url,
            "deadline": job.deadline.isoformat() if job.deadline else None,
        },
        headers={"trace_id": job.trace_id, "request_id": str(job.request_id)},
    )
    if is_native_job(job):
        def dispatch() -> None:
            _dispatch_native_job(str(job.id), queue_name)
        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            dispatch()
        else:
            transaction.on_commit(dispatch)


def _dispatch_native_job(job_id: str, queue_name: str) -> None:
    from apps.jobs.tasks import execute_job_task

    result = execute_job_task.apply_async(args=[job_id], queue=queue_name)
    Job.objects.filter(id=job_id).update(celery_task_id=result.id)
