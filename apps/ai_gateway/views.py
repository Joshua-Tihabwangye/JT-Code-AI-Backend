from __future__ import annotations

from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.ai_gateway.models import Evaluation, Model, ModelPolicy, ModelRun, Prompt, Provider
from apps.ai_gateway.serializers import (
    EvaluationCreateSerializer,
    EvaluationSerializer,
    ModelListSerializer,
    ModelPolicySerializer,
    ModelRunSerializer,
    ModelSerializer,
    PromptCreateSerializer,
    PromptSerializer,
    ProviderSerializer,
)
from apps.core.throttling import BurstThrottle, EmbeddingThrottle
from apps.core.views import APIView
from apps.events.outbox import enqueue_outbox_event


class ProviderViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ProviderSerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return Provider.objects.filter(status=Provider.Status.ACTIVE)


class ModelViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ModelSerializer
    lookup_field = 'id'

    def get_queryset(self):
        queryset = Model.objects.filter(
            status__in=[Model.Status.ACTIVE, Model.Status.BETA],
            provider__status=Provider.Status.ACTIVE
        ).select_related('provider')

        # Filter by modality
        modality = self.request.query_params.get('modality')
        if modality:
            queryset = queryset.filter(modality=modality)

        # Filter by provider
        provider = self.request.query_params.get('provider')
        if provider:
            queryset = queryset.filter(provider__slug=provider)

        # Filter by capabilities
        supports_tools = self.request.query_params.get('supports_tools')
        if supports_tools is not None:
            queryset = queryset.filter(supports_tools=supports_tools.lower() == 'true')

        supports_vision = self.request.query_params.get('supports_vision')
        if supports_vision is not None:
            queryset = queryset.filter(supports_vision=supports_vision.lower() == 'true')

        return queryset

    def get_serializer_class(self):
        if self.action == 'list':
            return ModelListSerializer
        return ModelSerializer


class ModelPolicyViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ModelPolicySerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return ModelPolicy.objects.filter(is_active=True).select_related(
            'primary_model', 'primary_model__provider'
        )

    @action(detail=False, methods=['get'])
    def for_task(self, request: Request):
        task_type = request.query_params.get('task_type')
        if not task_type:
            return Response({'detail': 'task_type parameter required'}, status=status.HTTP_400_BAD_REQUEST)

        policy = ModelPolicy.objects.filter(
            task_type=task_type,
            is_active=True,
            is_default=True
        ).select_related('primary_model', 'primary_model__provider').first()

        if not policy:
            # Fallback to any active policy for task type
            policy = ModelPolicy.objects.filter(
                task_type=task_type,
                is_active=True
            ).select_related('primary_model', 'primary_model__provider').first()

        if not policy:
            return Response({'detail': 'No policy found for task type'}, status=status.HTTP_404_NOT_FOUND)

        return Response(ModelPolicySerializer(policy).data)


class ModelRunViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ModelRunSerializer
    lookup_field = 'id'

    def get_queryset(self):
        user_orgs = self.request.user.organizations.values_list('id', flat=True)
        # Model runs are linked to jobs which have organization
        from apps.jobs.models import Job
        job_ids = Job.objects.filter(organization_id__in=user_orgs).values_list('id', flat=True)
        return ModelRun.objects.filter(
            Q(job_id__in=job_ids) | Q(request_id__in=[])
        ).select_related('provider', 'model', 'policy')


class PromptViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = PromptSerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return Prompt.objects.filter(is_active=True)

    def get_serializer_class(self):
        if self.action == 'create':
            return PromptCreateSerializer
        return PromptSerializer

    @action(detail=True, methods=['post'])
    def clone(self, request: Request, slug=None):
        prompt = self.get_object()
        new_prompt = Prompt.objects.create(
            name=f'{prompt.name} (Copy)',
            slug=f'{prompt.slug}-copy',
            category=prompt.category,
            content=prompt.content,
            variables=prompt.variables,
            description=prompt.description,
            model_constraints=prompt.model_constraints,
            version=1,
            is_active=True,
            tags=prompt.tags,
            metadata=prompt.metadata,
            created_by=request.user,
        )
        return Response(PromptSerializer(new_prompt).data, status=status.HTTP_201_CREATED)


class EvaluationViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = EvaluationSerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return Evaluation.objects.select_related('model', 'prompt', 'created_by')

    def get_serializer_class(self):
        if self.action == 'create':
            return EvaluationCreateSerializer
        return EvaluationSerializer

    @action(detail=True, methods=['post'])
    def run(self, request: Request, slug=None):
        evaluation = self.get_object()
        # Trigger evaluation run
        evaluation.status = Evaluation.Status.RUNNING
        evaluation.save(update_fields=['status'])

        enqueue_outbox_event(
            topic='ai_gateway.evaluation.run',
            event_key=str(evaluation.id),
            payload={
                'evaluation_id': str(evaluation.id),
                'model_id': str(evaluation.model_id),
                'prompt_id': str(evaluation.prompt_id),
                'dataset_name': evaluation.dataset_name,
                'dataset_version': evaluation.dataset_version,
            },
            headers={'trace_id': f'eval-{evaluation.id}'}
        )

        return Response({'detail': 'Evaluation started'})


class CompletionView(APIView):
    """AI completion endpoint - routes to appropriate model based on policy"""
    permission_classes = [IsAuthenticated]
    throttle_classes = [EmbeddingThrottle, BurstThrottle]

    def post(self, request: Request):
        task_type = request.data.get('task_type', 'GENERAL_QUESTION')
        messages = request.data.get('messages', [])
        model_id = request.data.get('model_id')
        policy_slug = request.data.get('policy_slug')
        stream = request.data.get('stream', False)
        temperature = request.data.get('temperature', 0.7)
        max_tokens = request.data.get('max_tokens')
        tools = request.data.get('tools', [])

        if not messages:
            return Response({'detail': 'messages required'}, status=status.HTTP_400_BAD_REQUEST)

        # Determine model to use
        if model_id:
            try:
                model = Model.objects.get(id=model_id, status__in=[Model.Status.ACTIVE, Model.Status.BETA])
            except Model.DoesNotExist:
                return Response({'detail': 'Model not found'}, status=status.HTTP_404_NOT_FOUND)
        else:
            # Use policy routing
            if policy_slug:
                try:
                    policy = ModelPolicy.objects.get(slug=policy_slug, is_active=True)
                except ModelPolicy.DoesNotExist:
                    return Response({'detail': 'Policy not found'}, status=status.HTTP_404_NOT_FOUND)
            else:
                policy = ModelPolicy.objects.filter(
                    task_type=task_type,
                    is_active=True,
                    is_default=True
                ).first()

            if not policy:
                return Response({'detail': 'No policy found for task type'}, status=status.HTTP_404_NOT_FOUND)

            model = policy.primary_model

        # Create job for completion
        from apps.jobs.models import Job
        job = Job.objects.create(
            owner=request.user,
            organization=request.user.organizations.first(),
            task_type=task_type,
            input_payload={
                'messages': messages,
                'model': model.name,
                'temperature': temperature,
                'max_tokens': max_tokens,
                'tools': tools,
                'stream': stream,
            },
        )

        # Reserve credits and enqueue
        from apps.jobs.views import JobViewSet
        viewset = JobViewSet()
        viewset._reserve_credits(job)
        viewset._enqueue_job(job)

        return Response({
            'job_id': str(job.id),
            'request_id': str(job.request_id),
            'status': 'queued',
            'model': model.name,
        }, status=status.HTTP_202_ACCEPTED)


class EmbeddingView(APIView):
    """Generate embeddings for text"""
    permission_classes = [IsAuthenticated]
    throttle_classes = [EmbeddingThrottle, BurstThrottle]

    def post(self, request: Request):
        texts = request.data.get('texts', [])
        model_id = request.data.get('model_id')

        if not texts:
            return Response({'detail': 'texts required'}, status=status.HTTP_400_BAD_REQUEST)

        if model_id:
            try:
                model = Model.objects.get(id=model_id, modality=Model.Modality.EMBEDDING)
            except Model.DoesNotExist:
                return Response({'detail': 'Embedding model not found'}, status=status.HTTP_404_NOT_FOUND)
        else:
            model = Model.objects.filter(
                modality=Model.Modality.EMBEDDING,
                status=Model.Status.ACTIVE,
                provider__status=Provider.Status.ACTIVE
            ).first()

        if not model:
            return Response(
            {'detail': 'No embedding model available'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

        # Create job for embedding
        from apps.jobs.models import Job
        job = Job.objects.create(
            owner=request.user,
            organization=request.user.organizations.first(),
            task_type=Job.TaskType.RAG_QUERY,  # Reuse or add EMBEDDING task type
            input_payload={
                'texts': texts,
                'model': model.name,
            },
        )

        from apps.jobs.views import JobViewSet
        viewset = JobViewSet()
        viewset._reserve_credits(job)
        viewset._enqueue_job(job)

        return Response({
            'job_id': str(job.id),
            'request_id': str(job.request_id),
            'status': 'queued',
        }, status=status.HTTP_202_ACCEPTED)


class AIModelsView(APIView):
    """List available models for current user's plan"""
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        user_orgs = request.user.organizations.all()
        if not user_orgs.exists():
            return Response({'models': []})

        # Get user's plan
        from apps.billing.models import Subscription
        subscription = Subscription.objects.filter(
            organization__in=user_orgs,
            status__in=[Subscription.Status.ACTIVE, Subscription.Status.TRIALING]
        ).select_related('plan').first()

        # Filter models based on plan
        # This would check plan entitlements for custom models, etc.
        models = Model.objects.filter(
            status__in=[Model.Status.ACTIVE, Model.Status.BETA],
            provider__status=Provider.Status.ACTIVE
        ).select_related('provider')

        return Response({
            'models': ModelListSerializer(models, many=True).data,
            'plan': subscription.plan.name if subscription else 'free',
        })