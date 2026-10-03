from __future__ import annotations

from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.throttling import BurstThrottle, EmbeddingThrottle
from apps.core.views import APIView
from apps.identity.authorization import (
    HasOrganizationWriteAccess,
    organization_for_request,
    tenant_scoped_queryset,
)
from apps.knowledge.access import accessible_chunks, accessible_documents
from apps.knowledge.models import Chunk, Citation, Collection, Document, RAGEvaluation, Source, SyncRun
from apps.knowledge.serializers import (
    ChunkSerializer,
    CitationSerializer,
    CollectionCreateSerializer,
    CollectionSerializer,
    DocumentSerializer,
    KnowledgeSearchRequestSerializer,
    RAGEvaluationRequestSerializer,
    RAGEvaluationSerializer,
    RAGQueryRequestSerializer,
    SourceCreateSerializer,
    SourceSerializer,
    SyncRunSerializer,
)


def _selected_organization_id(request: Request):
    """Return the explicitly selected tenant, or the legacy primary tenant."""
    organization = organization_for_request(request)
    return organization.id if organization is not None else None


def _tenant_queryset(queryset, request: Request, *, organization_field: str):
    """Scope a tenant-owned queryset to exactly one organization per request.

    This is necessary for operations such as vector search that take a single
    organization identifier and must never mix collections from tenants.
    """
    organization_id = _selected_organization_id(request)
    if organization_id is not None:
        return queryset.filter(**{f"{organization_field}_id": organization_id})
    return queryset.none()


class CollectionViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = CollectionSerializer
    lookup_field = "id"

    def get_queryset(self):
        return _tenant_queryset(
            Collection.objects.all(), self.request, organization_field="organization"
        ).select_related("organization", "created_by")

    def get_serializer_class(self):
        if self.action == "create":
            return CollectionCreateSerializer
        return CollectionSerializer

    def perform_create(self, serializer):
        organization = organization_for_request(self.request, required=True)
        serializer.save(organization=organization, created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def sync(self, request: Request, id=None):
        collection = self.get_object()
        from apps.knowledge.tasks import sync_source

        # Trigger each source directly; the task records the matching outbox event.
        sources = collection.sources.filter(is_active=True)
        for source in sources:
            sync_source.delay(str(source.id))
        return Response({"detail": f"Sync triggered for {sources.count()} sources"})


class SourceViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = SourceSerializer
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Source.objects.none()
        queryset = _tenant_queryset(
            Source.objects.all(), self.request, organization_field="collection__organization"
        )
        organization_id = _selected_organization_id(self.request)
        from apps.identity.authorization import user_has_role
        from apps.identity.models import Role

        if not (organization_id and user_has_role(self.request.user, Role.RoleType.ADMIN, organization_id)):
            documents = accessible_documents(
                Document.objects.all(), self.request.user, organization_id=organization_id
            )
            queryset = queryset.filter(
                Q(created_by=self.request.user) | Q(documents__in=documents)
            ).distinct()
        return queryset.select_related("collection", "collection__organization", "created_by")

    def get_serializer_class(self):
        if self.action == "create":
            return SourceCreateSerializer
        return SourceSerializer

    def perform_create(self, serializer):
        collection = serializer.validated_data["collection"]
        organization = organization_for_request(self.request, required=True)
        if collection.organization_id != organization.id:
            self.permission_denied(self.request, message="Collection is outside the selected organization.")
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def sync(self, request: Request, id=None):
        source = self.get_object()
        from apps.knowledge.tasks import sync_source

        sync_source.delay(str(source.id))
        return Response({"detail": "Sync triggered"})

    @action(detail=True, methods=["get"])
    def sync_runs(self, request: Request, id=None):
        source = self.get_object()
        runs = source.sync_runs.all()
        serializer = SyncRunSerializer(runs, many=True)
        return Response(serializer.data)


class DocumentViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = DocumentSerializer
    lookup_field = "id"

    def get_queryset(self):
        queryset = _tenant_queryset(
            Document.objects.all(), self.request, organization_field="collection__organization"
        )
        return accessible_documents(
            queryset, self.request.user, organization_id=_selected_organization_id(self.request)
        ).select_related("source", "collection")

    @action(detail=True, methods=["get"])
    def chunks(self, request: Request, id=None):
        document = self.get_object()
        chunks = document.chunks.all()
        page = self.paginate_queryset(chunks)
        if page is not None:
            serializer = ChunkSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = ChunkSerializer(chunks, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def reindex(self, request: Request, id=None):
        """Clear existing embeddings and re-run the full indexing pipeline."""
        document = self.get_object()
        from apps.knowledge.tasks import process_document
        from apps.knowledge.vectorstore import delete_document_embeddings, vector_store_enabled

        if vector_store_enabled():
            delete_document_embeddings(document.id)
        document.status = document.Status.PENDING
        document.indexed_at = None
        document.chunk_count = 0
        document.vector_ids = []
        document.last_error = ""
        save_fields = [
            "status",
            "indexed_at",
            "chunk_count",
            "vector_ids",
            "last_error",
            "updated_at",
        ]
        document.save(update_fields=save_fields)
        process_document.delay(str(document.id))
        body = {
            "detail": "Re-index queued",
            "document_id": str(document.id),
        }
        return Response(body, status=status.HTTP_202_ACCEPTED)


class ChunkViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ChunkSerializer
    lookup_field = "id"

    def get_queryset(self):
        queryset = _tenant_queryset(
            Chunk.objects.all(), self.request, organization_field="collection__organization"
        )
        return accessible_chunks(
            queryset, self.request.user, organization_id=_selected_organization_id(self.request)
        ).select_related("document", "collection")


class SyncRunViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = SyncRunSerializer
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return SyncRun.objects.none()
        sources = SourceViewSet()
        sources.request = self.request
        allowed_sources = sources.get_queryset()
        return (
            _tenant_queryset(
                SyncRun.objects.all(), self.request, organization_field="source__collection__organization"
            )
            .filter(source__in=allowed_sources)
            .select_related("source", "source__collection")
        )


class CitationViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = CitationSerializer
    lookup_field = "id"

    def get_queryset(self):
        queryset = _tenant_queryset(
            Citation.objects.all(), self.request, organization_field="job__organization"
        )
        return queryset.filter(
            document__in=accessible_documents(
                Document.objects.all(),
                self.request.user,
                organization_id=_selected_organization_id(self.request),
            )
        ).select_related("job", "document", "chunk")


class SearchView(APIView):
    """Semantic (pgvector) search across collections stored in Supabase."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [EmbeddingThrottle, BurstThrottle]

    @extend_schema(request=KnowledgeSearchRequestSerializer)
    def post(self, request: Request):
        serializer = KnowledgeSearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        query = serializer.validated_data["query"].strip()
        collection_ids = serializer.validated_data["collection_ids"]
        top_k = serializer.validated_data["top_k"]
        min_similarity = serializer.validated_data.get("min_similarity")

        # Resolve collections inside exactly one selected tenant before touching
        # the vector store, which accepts only one organization identifier.
        organization = organization_for_request(request, required=True)
        collections = Collection.objects.filter(organization=organization, is_active=True)
        if collection_ids:
            collections = collections.filter(id__in=collection_ids)

        if not collections.exists():
            return Response({"results": [], "message": "No accessible collections"})

        allowed_ids = list(collections.values_list("id", flat=True))
        organization_id = collections.first().organization_id

        try:
            from apps.knowledge.embeddings import EmbeddingError, embed_query
            from apps.knowledge.retrieval import hybrid_search
            from apps.knowledge.vectorstore import VectorStoreUnavailable

            try:
                query_vector = embed_query(query)
            except EmbeddingError:
                query_vector = None
            results = hybrid_search(
                query,
                query_vector,
                collection_ids=allowed_ids,
                organization_id=organization_id,
                user=request.user,
                top_k=top_k,
                min_similarity=min_similarity,
            )
        except VectorStoreUnavailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response(
            {
                "query": query,
                "collections": allowed_ids,
                "results": results,
                "result_count": len(results),
            }
        )


class RAGQueryView(APIView):
    """RAG query with grounded generation"""

    permission_classes = [IsAuthenticated]
    throttle_classes = [EmbeddingThrottle, BurstThrottle]

    @extend_schema(request=RAGQueryRequestSerializer)
    def post(self, request: Request):
        serializer = RAGQueryRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        query = serializer.validated_data["query"].strip()
        collection_ids = list(dict.fromkeys(serializer.validated_data["collection_ids"]))
        conversation_id = serializer.validated_data.get("conversation_id")
        include_citations = serializer.validated_data["include_citations"]

        # The job and every collection must belong to the selected tenant.
        organization = organization_for_request(request, required=True)
        collections = Collection.objects.filter(
            id__in=collection_ids, organization=organization, is_active=True
        )

        if collections.count() != len(collection_ids):
            return Response({"detail": "No accessible collections"}, status=status.HTTP_403_FORBIDDEN)

        if conversation_id:
            from apps.conversations.models import Conversation

            conversation_exists = tenant_scoped_queryset(
                Conversation.objects.filter(id=conversation_id),
                request.user,
                organization_id=organization.id,
            ).exists()
            if not conversation_exists:
                return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        # Create job for RAG query
        from apps.jobs.models import Job

        job = Job.objects.create(
            owner=request.user,
            organization=organization,
            task_type=Job.TaskType.RAG_QUERY,
            input_payload={
                "query": query,
                "collection_ids": [str(c.id) for c in collections],
                "conversation_id": conversation_id,
                "include_citations": include_citations,
            },
        )

        # Enqueue for processing
        from apps.jobs.views import JobViewSet

        viewset = JobViewSet()
        viewset.request = request
        viewset._reserve_credits(job)
        viewset._enqueue_job(job)

        return Response(
            {"job_id": str(job.id), "request_id": str(job.request_id), "status": "queued"},
            status=status.HTTP_202_ACCEPTED,
        )


class RAGEvaluationView(APIView):
    """Evaluate an authorized retrieval result against an expected evidence set."""

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]

    @extend_schema(request=RAGEvaluationRequestSerializer, responses={201: RAGEvaluationSerializer})
    def post(self, request: Request):
        serializer = RAGEvaluationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        query = serializer.validated_data["query"].strip()
        sources = serializer.validated_data["sources"]
        answer = serializer.validated_data["answer"]
        expected = serializer.validated_data["expected_chunk_ids"]
        organization = organization_for_request(request, required=True)
        supplied_ids = [item.get("chunk_id") for item in sources if isinstance(item, dict)]
        allowed_chunks = accessible_chunks(
            Chunk.objects.filter(
                id__in=supplied_ids,
                document__status=Document.Status.INDEXED,
                collection__is_active=True,
                document__source__is_active=True,
            ).select_related("document"),
            request.user,
            organization_id=organization.id,
        )
        by_id = {str(chunk.id): chunk for chunk in allowed_chunks}
        safe_sources = []
        for supplied in sources:
            chunk = by_id.get(str(supplied.get("chunk_id")))
            if chunk is None:
                continue
            safe_sources.append(
                {
                    "chunk_id": str(chunk.id),
                    "document_id": str(chunk.document_id),
                    "document_title": chunk.document.title,
                    "collection_id": str(chunk.collection_id),
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.content,
                    "score": 0.0,
                }
            )
        from apps.knowledge.retrieval import evaluate_response

        evaluation = evaluate_response(
            organization=organization,
            query=query,
            sources=safe_sources,
            answer=answer,
            expected_chunk_ids=expected,
            user=request.user,
        )
        return Response(evaluation, status=status.HTTP_201_CREATED)


class RAGEvaluationViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = RAGEvaluationSerializer
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return RAGEvaluation.objects.none()
        queryset = _tenant_queryset(
            RAGEvaluation.objects.all(), self.request, organization_field="organization"
        )
        organization_id = _selected_organization_id(self.request)
        from apps.identity.authorization import user_can_edit_organization

        if organization_id and user_can_edit_organization(self.request.user, organization_id):
            return queryset
        return queryset.filter(created_by=self.request.user)
