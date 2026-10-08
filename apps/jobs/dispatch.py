"""Durable job dispatch and workload-to-queue policy."""

from __future__ import annotations

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
        Job.TaskType.KNOWLEDGE_INGESTION,
        Job.TaskType.RAG_QUERY,
        Job.TaskType.SEARCH_RESEARCH,
    }
)


def supported_task_types() -> set[str]:
    """Native task types plus those claimed by a deployed n8n workflow (when n8n is configured)."""
    from apps.orchestration import client
    from apps.orchestration.registry import n8n_task_types

    return set(NATIVE_TASK_TYPES) | (n8n_task_types() if client.configured() else set())


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


@transaction.atomic
def enqueue_job(job: Job) -> None:
    """Persist dispatch intent before making work visible to a worker or integration."""
    from apps.orchestration.registry import n8n_task_types

    orchestrated = job.task_type in n8n_task_types()
    queue_name = "orchestration" if orchestrated else queue_for_task_type(job.task_type)
    if job.queue_name != queue_name:
        job.queue_name = queue_name
        job.save(update_fields=["queue_name", "updated_at"])
    if orchestrated:
        from apps.orchestration.runs import start_job_workflow

        start_job_workflow(job)
    else:
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

    # Every accepted generic Job must reach a terminal state. The worker runs
    # supported handlers and explicitly fails unsupported types; no job is
    # left indefinitely queued awaiting an undocumented external consumer.
    def dispatch() -> None:
        _dispatch_job(str(job.id), queue_name)

    if not orchestrated:  # n8n jobs are dispatched by apps.orchestration.runs
        transaction.on_commit(dispatch)


def _dispatch_job(job_id: str, queue_name: str) -> bool:
    from apps.jobs.tasks import execute_job_task

    try:
        result = execute_job_task.apply_async(args=[job_id], queue=queue_name)
    except Exception:  # broker publication is recovered by dispatch_queued_jobs
        return False
    Job.objects.filter(id=job_id, status=Job.Status.QUEUED).update(celery_task_id=result.id)
    return True
