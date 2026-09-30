from __future__ import annotations

import secrets
import uuid
from decimal import Decimal

from celery import current_app
from django.conf import settings
from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.billing.services import CreditService
from apps.core.throttling import BurstThrottle, ResearchThrottle
from apps.core.views import APIView
from apps.identity.authorization import (
    HasOrganizationWriteAccess,
    organization_for_request,
    tenant_scoped_queryset,
)
from apps.jobs.dispatch import NATIVE_TASK_TYPES, enqueue_job
from apps.jobs.metrics import queue_depths
from apps.jobs.models import Callback, Job, JobStep, WorkflowRun
from apps.jobs.serializers import (
    CallbackSerializer,
    JobCreateSerializer,
    JobSerializer,
    JobStatusUpdateSerializer,
    JobStepSerializer,
    WorkflowRunSerializer,
)
from apps.jobs.transitions import InvalidJobTransition, apply_status_update


class PaymentRequired(APIException):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    default_detail = "Insufficient credits for this job."
    default_code = "insufficient_credits"


class JobViewSet(viewsets.ModelViewSet):
    """Jobs are created and read by clients; state changes only via cancel/retry/worker paths."""

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = JobSerializer
    lookup_field = "id"
    # No PUT/PATCH/DELETE: status, billing and results are owned by the durable
    # state machine (apps.jobs.transitions), never by client writes.
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        return (
            tenant_scoped_queryset(Job.objects.all(), self.request.user)
            .select_related("organization", "conversation", "workflow_run")
            .prefetch_related("steps__provider_attempts", "callbacks")
        )

    def get_serializer_class(self):
        if self.action == "create":
            return JobCreateSerializer
        return JobSerializer

    def perform_create(self, serializer):
        with transaction.atomic():
            job = serializer.save(
                owner=self.request.user,
                organization=organization_for_request(self.request, required=True),
                trace_id=getattr(self.request, "trace_id", ""),
            )
            self._reserve_credits(job)
            self._enqueue_job(job)

    def _reserve_credits(self, job: Job):
        # Calculate estimated cost based on task type
        estimated_credits = self._estimate_credits(job.task_type, job.input_payload)
        job.reserved_credits = estimated_credits
        job.save(update_fields=["reserved_credits"])

        # Reserve credits from the tenant wallet; the surrounding transaction
        # rolls the job back when the wallet cannot cover the estimate.
        try:
            CreditService.reserve_credits(
                user=self.request.user,
                amount=estimated_credits,
                request_id=job.request_id,
                job_id=job.id,
                reason=f"Job reservation: {job.task_type}",
                organization=job.organization,
            )
        except ValueError as exc:
            raise PaymentRequired(str(exc)) from exc

    def _estimate_credits(self, task_type: str, input_payload: dict) -> Decimal:
        # Simple estimation based on task type
        estimates = {
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
        return estimates.get(task_type, Decimal("10"))

    def _enqueue_job(self, job: Job):
        enqueue_job(job)

    @action(detail=True, methods=["post"])
    def cancel(self, request: Request, id=None):
        current_job = self.get_object()
        if current_job.status not in [
            Job.Status.QUEUED,
            Job.Status.RUNNING,
            Job.Status.VALIDATING,
            Job.Status.WAITING_APPROVAL,
        ]:
            return Response(
                {"detail": "Job cannot be cancelled in current status"}, status=status.HTTP_400_BAD_REQUEST
            )
        try:
            job, changed = apply_status_update(current_job.id, {"status": Job.Status.CANCELLED})
        except InvalidJobTransition:
            return Response(
                {"detail": "Job cannot be cancelled in current status"}, status=status.HTTP_409_CONFLICT
            )
        # Revoke only after the durable cancellation and its outbox record commit.
        if job.celery_task_id:
            current_app.control.revoke(job.celery_task_id, terminate=False)
        return Response(JobSerializer(job, context={"request": request}).data)

    @transaction.atomic
    @action(detail=True, methods=["post"])
    def retry(self, request: Request, id=None):
        job = self.get_object()
        if job.status not in [Job.Status.FAILED, Job.Status.CANCELLED, Job.Status.EXPIRED]:
            return Response(
                {"detail": "Job can only be retried from failed/cancelled/expired status"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if job.task_type not in NATIVE_TASK_TYPES:
            return Response(
                {"detail": "This task type is not supported by the job runtime."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Create new job with same parameters
        new_job = Job.objects.create(
            owner=job.owner,
            organization=job.organization,
            conversation=job.conversation,
            task_type=job.task_type,
            input_payload=job.input_payload,
            callback_url=job.callback_url,
            deadline=job.deadline,
            status=Job.Status.QUEUED,
            trace_id=getattr(request, "trace_id", ""),
        )

        self._reserve_credits(new_job)
        self._enqueue_job(new_job)

        return Response(
            JobSerializer(new_job, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"])
    def my_jobs(self, request: Request):
        """Get current user's jobs with filtering"""
        queryset = self.get_queryset()
        task_type = request.query_params.get("task_type")
        job_status = request.query_params.get("status")

        if task_type:
            queryset = queryset.filter(task_type=task_type)
        if job_status:
            queryset = queryset.filter(status=job_status)

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="queue-metrics")
    def queue_metrics(self, request: Request) -> Response:
        if not request.user.is_staff:
            raise PermissionDenied("Staff access is required for queue metrics.")
        return Response({"queues": queue_depths()})


class JobStepViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = JobStepSerializer
    lookup_field = "id"

    def get_queryset(self):
        return tenant_scoped_queryset(
            JobStep.objects.all(),
            self.request.user,
            organization_field="job__organization",
            owner_field="job__owner",
        ).select_related("job")


class WorkflowRunViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WorkflowRunSerializer
    lookup_field = "id"

    def get_queryset(self):
        return tenant_scoped_queryset(
            WorkflowRun.objects.all(),
            self.request.user,
            organization_field="job__organization",
            owner_field="job__owner",
        ).select_related("job")


class CallbackViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = CallbackSerializer
    lookup_field = "id"

    def get_queryset(self):
        return tenant_scoped_queryset(
            Callback.objects.all(),
            self.request.user,
            organization_field="job__organization",
            owner_field="job__owner",
        ).select_related("job")


class JobStatusCallbackView(APIView):
    """Callback endpoint for n8n to update job status"""

    permission_classes = []
    authentication_classes = []

    def post(self, request: Request, job_id: uuid.UUID):
        secret = request.headers.get("X-JT-Code-Webhook-Secret")
        expected_secret = settings.N8N_WEBHOOK_SECRET
        if not expected_secret or not secret or not secrets.compare_digest(secret, expected_secret):
            return Response({"detail": "Invalid webhook secret"}, status=status.HTTP_401_UNAUTHORIZED)
        serializer = JobStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            job, _ = apply_status_update(job_id, serializer.validated_data)
        except Job.DoesNotExist:
            return Response({"detail": "Job not found"}, status=status.HTTP_404_NOT_FOUND)
        except InvalidJobTransition as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(JobSerializer(job, context={"request": request}).data)


class ResearchJobsView(APIView):
    """Start a deep research job with cost estimate, source policy and async execution."""

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    throttle_classes = [ResearchThrottle, BurstThrottle]

    @transaction.atomic
    def post(self, request: Request) -> Response:
        query = (request.data.get("query") or "").strip()
        if not query:
            return Response({"detail": "query is required"}, status=status.HTTP_400_BAD_REQUEST)
        if len(query) > 2000:
            return Response(
                {"detail": "Query exceeds the 2000 character limit."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        collection_ids = request.data.get("collection_ids") or []
        depth = request.data.get("depth", "standard")
        if depth not in {"standard", "deep"}:
            return Response(
                {"detail": "depth must be standard or deep."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        organization = organization_for_request(request, required=True)
        collections = []
        if collection_ids:
            from apps.knowledge.models import Collection

            collections = list(
                Collection.objects.filter(
                    id__in=collection_ids,
                    organization=organization,
                    is_active=True,
                ).values_list("id", flat=True)
            )
        if not collection_ids:
            from apps.knowledge.models import Collection

            collections = list(
                Collection.objects.filter(
                    organization=organization,
                    is_active=True,
                ).values_list("id", flat=True)
            )

        estimated_credits = Decimal("150") if depth == "deep" else Decimal("75")
        request_id = uuid.uuid4()
        try:
            CreditService.reserve_credits(
                user=request.user,
                amount=estimated_credits,
                request_id=request_id,
                reason=f"Deep research: {depth}",
                organization=organization,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)

        job = Job.objects.create(
            owner=request.user,
            organization=organization,
            task_type=Job.TaskType.SEARCH_RESEARCH,
            idempotency_key=f"research:{request_id}",
            input_payload={
                "query": query,
                "collection_ids": [str(c) for c in collections],
                "depth": depth,
            },
            reserved_credits=estimated_credits,
            request_id=request_id,
            trace_id=getattr(request, "trace_id", ""),
        )

        enqueue_job(job)

        return Response(
            {
                "job_id": str(job.id),
                "request_id": str(job.request_id),
                "status": "queued",
                "estimated_credits": str(estimated_credits),
                "query": query,
                "depth": depth,
                "collections": [str(c) for c in collections],
            },
            status=status.HTTP_202_ACCEPTED,
        )
