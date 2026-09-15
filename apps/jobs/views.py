from __future__ import annotations

import uuid
from decimal import Decimal

from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.billing.services import CreditService
from apps.core.throttling import BurstThrottle, ResearchThrottle
from apps.core.views import APIView
from apps.events.outbox import enqueue_outbox_event
from apps.jobs.models import Callback, Job, JobStep, WorkflowRun
from apps.jobs.serializers import (
    CallbackSerializer,
    JobCreateSerializer,
    JobSerializer,
    JobStatusUpdateSerializer,
    JobStepSerializer,
    WorkflowRunSerializer,
)


class JobViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = JobSerializer
    lookup_field = 'id'

    def get_queryset(self):
        return Job.objects.filter(owner=self.request.user).select_related(
            'organization', 'conversation', 'workflow_run'
        ).prefetch_related('steps__provider_attempts', 'callbacks')

    def get_serializer_class(self):
        if self.action == 'create':
            return JobCreateSerializer
        return JobSerializer

    def perform_create(self, serializer):
        job = serializer.save(owner=self.request.user)
        # Reserve credits for the job
        self._reserve_credits(job)
        # Enqueue job for n8n processing
        self._enqueue_job(job)

    def _reserve_credits(self, job: Job):
        # Calculate estimated cost based on task type
        estimated_credits = self._estimate_credits(job.task_type, job.input_payload)
        job.reserved_credits = estimated_credits
        job.save(update_fields=['reserved_credits'])

        # Reserve credits from user's wallet
        CreditService.reserve_credits(
            user=self.request.user,
            amount=estimated_credits,
            request_id=job.request_id,
            job_id=job.id,
            reason=f'Job reservation: {job.task_type}'
        )

    def _estimate_credits(self, task_type: str, input_payload: dict) -> Decimal:
        # Simple estimation based on task type
        estimates = {
            Job.TaskType.GENERAL_QUESTION: Decimal('10'),
            Job.TaskType.IMAGE_UNDERSTANDING: Decimal('50'),
            Job.TaskType.IMAGE_GENERATION: Decimal('100'),
            Job.TaskType.DOCUMENT_DRAFTING: Decimal('30'),
            Job.TaskType.DOCUMENT_RENDERING: Decimal('20'),
            Job.TaskType.FILE_CONVERSION: Decimal('15'),
            Job.TaskType.SEARCH_RESEARCH: Decimal('40'),
            Job.TaskType.RAG_QUERY: Decimal('25'),
            Job.TaskType.KNOWLEDGE_INGESTION: Decimal('100'),
            Job.TaskType.SCHEDULED_AUTOMATION: Decimal('10'),
        }
        return estimates.get(task_type, Decimal('10'))

    def _enqueue_job(self, job: Job):
        # Create workflow run record
        WorkflowRun.objects.create(
            job=job,
            n8n_workflow_id=f'{job.task_type.lower()}',
            input_payload=job.input_payload,
        )

        # Enqueue outbox event for n8n
        enqueue_outbox_event(
            topic=f'{job._meta.model._meta.app_label}.job.created',
            event_key=str(job.request_id),
            payload={
                'job_id': str(job.id),
                'request_id': str(job.request_id),
                'task_type': job.task_type,
                'input_payload': job.input_payload,
                'owner_id': str(job.owner_id),
                'organization_id': str(job.organization_id) if job.organization_id else None,
                'reserved_credits': str(job.reserved_credits),
                'callback_url': job.callback_url,
                'deadline': job.deadline.isoformat() if job.deadline else None,
            },
            headers={
                'trace_id': job.trace_id,
                'request_id': str(job.request_id),
            }
        )

    @action(detail=True, methods=['post'])
    def cancel(self, request: Request, id=None):
        job = self.get_object()
        if job.status not in [Job.Status.QUEUED, Job.Status.RUNNING, Job.Status.VALIDATING]:
            return Response(
                {'detail': 'Job cannot be cancelled in current status'},
                status=status.HTTP_400_BAD_REQUEST
            )

        job.status = Job.Status.CANCELLED
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'completed_at'])

        # Release reserved credits
        CreditService.release_reservation(
            user=request.user,
            request_id=job.request_id
        )

        # Enqueue cancellation event
        enqueue_outbox_event(
            topic='jobs.job.cancelled',
            event_key=str(job.request_id),
            payload={'job_id': str(job.id), 'request_id': str(job.request_id)},
            headers={'trace_id': job.trace_id}
        )

        return Response(JobSerializer(job, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def retry(self, request: Request, id=None):
        job = self.get_object()
        if job.status not in [Job.Status.FAILED, Job.Status.CANCELLED, Job.Status.EXPIRED]:
            return Response(
                {'detail': 'Job can only be retried from failed/cancelled/expired status'},
                status=status.HTTP_400_BAD_REQUEST
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
        )

        self._reserve_credits(new_job)
        self._enqueue_job(new_job)

        return Response(
            JobSerializer(new_job, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=['get'])
    def my_jobs(self, request: Request):
        """Get current user's jobs with filtering"""
        queryset = self.get_queryset()
        task_type = request.query_params.get('task_type')
        job_status = request.query_params.get('status')

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


class JobStepViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = JobStepSerializer
    lookup_field = 'id'

    def get_queryset(self):
        return JobStep.objects.filter(job__owner=self.request.user).select_related('job')


class WorkflowRunViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WorkflowRunSerializer
    lookup_field = 'id'

    def get_queryset(self):
        return WorkflowRun.objects.filter(job__owner=self.request.user).select_related('job')


class CallbackViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = CallbackSerializer
    lookup_field = 'id'

    def get_queryset(self):
        return Callback.objects.filter(job__owner=self.request.user).select_related('job')


class JobStatusCallbackView(APIView):
    """Callback endpoint for n8n to update job status"""
    permission_classes = []
    authentication_classes = []

    def post(self, request: Request, job_id: uuid.UUID):
        # Verify webhook signature
        secret = request.headers.get('X-JT-Code-Webhook-Secret')
        if secret != self.settings.N8N_WEBHOOK_SECRET:
            return Response({'detail': 'Invalid webhook secret'}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            job = Job.objects.get(id=job_id)
        except Job.DoesNotExist:
            return Response({'detail': 'Job not found'}, status=status.HTTP_404_NOT_FOUND)

        serializer = JobStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        old_status = job.status
        job.status = data['status']

        if data['status'] in [Job.Status.COMPLETED, Job.Status.FAILED, Job.Status.CANCELLED]:
            job.completed_at = timezone.now()

        if 'result' in data:
            job.result = data['result']
        if 'error_code' in data:
            job.error_code = data['error_code']
        if 'error_message' in data:
            job.error_message = data['error_message']
        if 'n8n_execution_id' in data:
            job.n8n_execution_id = data['n8n_execution_id']

        job.save()

        # Update workflow run if exists
        if hasattr(job, 'workflow_run'):
            wr = job.workflow_run
            if 'progress_percent' in data:
                wr.progress_percent = data['progress_percent']
            if 'steps_completed' in data:
                wr.steps_completed = data['steps_completed']
            if 'total_steps' in data:
                wr.total_steps = data['total_steps']
            if data['status'] in [Job.Status.COMPLETED, Job.Status.FAILED]:
                wr.status = data['status']
                wr.completed_at = timezone.now()
                if 'result' in data:
                    wr.output_payload = data['result']
                if 'error_message' in data:
                    wr.error_message = data['error_message']
            wr.save()

        # Handle credit settlement
        if data['status'] == Job.Status.COMPLETED:
            actual_credits = data.get('actual_credits', job.reserved_credits)
            job.actual_credits = actual_credits
            job.save(update_fields=['actual_credits'])

            CreditService.settle_reservation(
                user=job.owner,
                request_id=job.request_id,
                actual_amount=actual_credits
            )
        elif data['status'] in [Job.Status.FAILED, Job.Status.CANCELLED]:
            CreditService.release_reservation(
                user=job.owner,
                request_id=job.request_id
            )

        # Enqueue completion event
        enqueue_outbox_event(
            topic=f'jobs.job.{data["status"]}',
            event_key=str(job.request_id),
            payload={
                'job_id': str(job.id),
                'request_id': str(job.request_id),
                'old_status': old_status,
                'new_status': data['status'],
                'result': data.get('result'),
                'error': data.get('error_message'),
            },
            headers={'trace_id': job.trace_id}
        )

        return Response(JobSerializer(job).data)

class ResearchJobsView(APIView):
    """Start a deep research job with cost estimate, source policy and async execution."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [ResearchThrottle, BurstThrottle]

    def post(self, request: Request) -> Response:
        query = (request.data.get('query') or '').strip()
        if not query:
            return Response({'detail': 'query is required'}, status=status.HTTP_400_BAD_REQUEST)
        if len(query) > 2000:
            return Response(
                {'detail': 'Query exceeds the 2000 character limit.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        collection_ids = request.data.get('collection_ids') or []
        depth = request.data.get('depth', 'standard')
        if depth not in {'standard', 'deep'}:
            return Response(
                {'detail': 'depth must be standard or deep.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_orgs = request.user.organizations.values_list('id', flat=True)
        collections = []
        if collection_ids:
            from apps.knowledge.models import Collection
            collections = list(
                Collection.objects.filter(
                    id__in=collection_ids,
                    organization_id__in=user_orgs,
                    is_active=True,
                ).values_list('id', flat=True)
            )
        if not collection_ids:
            from apps.knowledge.models import Collection
            collections = list(
                Collection.objects.filter(
                    organization_id__in=user_orgs,
                    is_active=True,
                ).values_list('id', flat=True)
            )

        estimated_credits = Decimal('150') if depth == 'deep' else Decimal('75')
        request_id = uuid.uuid4()
        try:
            CreditService.reserve_credits(
                user=request.user,
                amount=estimated_credits,
                request_id=request_id,
                reason=f'Deep research: {depth}',
            )
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)

        job = Job.objects.create(
            owner=request.user,
            organization=request.user.organizations.first(),
            task_type=Job.TaskType.SEARCH_RESEARCH,
            idempotency_key=f'research:{request_id}',
            input_payload={
                'query': query,
                'collection_ids': [str(c) for c in collections],
                'depth': depth,
            },
            reserved_credits=estimated_credits,
            request_id=request_id,
        )

        enqueue_outbox_event(
            topic='jobs.job.created',
            event_key=str(job.request_id),
            payload={
                'job_id': str(job.id),
                'request_id': str(job.request_id),
                'task_type': job.task_type,
                'query': query,
                'depth': depth,
                'collection_ids': [str(c) for c in collections],
                'owner_id': str(job.owner_id),
                'reserved_credits': str(estimated_credits),
            },
        )

        return Response({
            'job_id': str(job.id),
            'request_id': str(job.request_id),
            'status': 'queued',
            'estimated_credits': str(estimated_credits),
            'query': query,
            'depth': depth,
            'collections': [str(c) for c in collections],
        }, status=status.HTTP_202_ACCEPTED)
