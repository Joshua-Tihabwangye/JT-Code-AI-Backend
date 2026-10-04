"""Credit reservation shared by every API surface that creates billable jobs."""

from __future__ import annotations

from decimal import Decimal

from rest_framework import status
from rest_framework.exceptions import APIException

from apps.billing.services import CreditService
from apps.jobs.models import Job

# Up-front reservation per task type; settlement charges actual usage. Plan
# quotas and model-based estimates replace this table in Phase 13.
CREDIT_ESTIMATES: dict[str, Decimal] = {
    Job.TaskType.GENERAL_QUESTION: Decimal("10"),
    Job.TaskType.IMAGE_UNDERSTANDING: Decimal("50"),
    Job.TaskType.IMAGE_GENERATION: Decimal("100"),
    Job.TaskType.DOCUMENT_DRAFTING: Decimal("30"),
    Job.TaskType.DOCUMENT_RENDERING: Decimal("20"),
    Job.TaskType.FILE_CONVERSION: Decimal("15"),
    Job.TaskType.SEARCH_RESEARCH: Decimal("40"),
    Job.TaskType.RAG_QUERY: Decimal("25"),
    Job.TaskType.KNOWLEDGE_INGESTION: Decimal("100"),
    Job.TaskType.SCHEDULED_AUTOMATION: Decimal("10"),
}


class PaymentRequired(APIException):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    default_detail = "Insufficient credits for this job."
    default_code = "insufficient_credits"


def estimate_credits(task_type: str) -> Decimal:
    return CREDIT_ESTIMATES.get(task_type, Decimal("10"))


def reserve_job_credits(job: Job, user) -> None:
    """Reserve the job's estimate from the tenant wallet (raises ``PaymentRequired``)."""
    job.reserved_credits = estimate_credits(job.task_type)
    job.save(update_fields=["reserved_credits"])
    try:
        CreditService.reserve_credits(
            user=user,
            amount=job.reserved_credits,
            request_id=job.request_id,
            job_id=job.id,
            reason=f"Job reservation: {job.task_type}",
            organization=job.organization,
        )
    except ValueError as exc:
        raise PaymentRequired(str(exc)) from exc
