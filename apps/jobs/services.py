"""Metering for jobs: concurrency, quota and credit reservation at creation."""

from __future__ import annotations

from typing import Any

from apps.jobs.models import Job
from apps.usage import services as metering
from apps.usage.concurrency import enforce_concurrency
from apps.usage.exceptions import InsufficientCredits as PaymentRequired
from apps.usage.models import Feature

__all__ = ["JOB_FEATURES", "PaymentRequired", "reserve_job_credits", "settle_job_usage"]

JOB_FEATURES: dict[str, str] = {
    Job.TaskType.GENERAL_QUESTION: Feature.CHAT_MESSAGES,
    Job.TaskType.RAG_QUERY: Feature.RAG_QUERIES,
    Job.TaskType.SEARCH_RESEARCH: Feature.SEARCH_QUERIES,
    Job.TaskType.KNOWLEDGE_INGESTION: Feature.KNOWLEDGE_DOCUMENTS,
    Job.TaskType.IMAGE_GENERATION: Feature.IMAGE_GENERATIONS,
    Job.TaskType.IMAGE_UNDERSTANDING: Feature.IMAGE_GENERATIONS,
    Job.TaskType.DOCUMENT_DRAFTING: Feature.DOCUMENT_RENDERS,
    Job.TaskType.DOCUMENT_RENDERING: Feature.DOCUMENT_RENDERS,
    Job.TaskType.FILE_CONVERSION: Feature.FILE_CONVERSIONS,
    Job.TaskType.SCHEDULED_AUTOMATION: Feature.WORKFLOW_EXECUTIONS,
}


def reserve_job_credits(job: Job, user: Any) -> None:
    """Enforce the tenant's job concurrency and hold credits for ``job``.

    Call inside the transaction that created the job so a refusal rolls it back.
    """
    enforce_concurrency(job.organization, "jobs", exclude=job.pk)
    reservation = metering.reserve(
        organization=job.organization,
        user=user,
        feature=JOB_FEATURES.get(job.task_type, Feature.WORKFLOW_EXECUTIONS),
        source_type="job",
        source_id=job.id,
    )
    job.reserved_credits = reservation.credits_reserved
    job.save(update_fields=["reserved_credits", "updated_at"])


def settle_job_usage(job: Job, *, reported_credits: Any = None) -> None:
    """Settle a finished job from its model-run cost, or release its hold.

    ``reported_credits`` (from the signed integration callback for externally
    executed jobs) replaces the computed price; it is still capped at the hold.
    """
    from apps.usage.models import UsageRecord

    reservation = metering.reservation_for("job", job.id)
    if reported_credits is not None and job.status == Job.Status.COMPLETED and reservation is not None:
        metering.settle(reservation.id, credits=reported_credits)
    else:
        metering.finalize_source("job", job.id)
    record = UsageRecord.objects.filter(source_type="job", source_id=str(job.id)).first()
    if record is not None and job.actual_credits != record.credits_charged:
        Job.objects.filter(id=job.id).update(actual_credits=record.credits_charged)
        job.actual_credits = record.credits_charged
