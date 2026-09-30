from __future__ import annotations

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.throttling import BurstThrottle, EmbeddingThrottle
from apps.core.views import APIView
from apps.events.outbox import enqueue_outbox_event
from apps.identity.authorization import (
    HasOrganizationWriteAccess,
    organization_for_request,
    tenant_scoped_queryset,
)
from apps.knowledge.models import Chunk, Citation, Collection, Document, Source, SyncRun
from apps.knowledge.serializers import (
    ChunkSerializer,
    CitationSerializer,
    CollectionCreateSerializer,
    CollectionSerializer,
    DocumentSerializer,
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
        # Trigger sync for all active sources
        sources = collection.sources.filter(is_active=True)
        for source in sources:
            enqueue_outbox_event(
                topic="knowledge.source.sync",
                event_key=str(source.id),
                payload={
                    "source_id": str(source.id),
                    "collection_id": str(collection.id),
                    "organization_id": str(collection.organization_id),
                },
                headers={"trace_id": f"sync-{collection.id}"},
            )
        return Response({"detail": f"Sync triggered for {sources.count()} sources"})


class SourceViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = SourceSerializer
    lookup_field = "id"

    def get_queryset(self):
        return _tenant_queryset(
            Source.objects.all(), self.request, organization_field="collection__organization"
        ).select_related("collection", "collection__organization", "created_by")

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
        enqueue_outbox_event(
            topic="knowledge.source.sync",
            event_key=str(source.id),
            payload={
                "source_id": str(source.id),
                "collection_id": str(source.collection_id),
                "organization_id": str(source.collection.organization_id),
            },
            headers={"trace_id": f"sync-{source.id}"},
        )
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
        return _tenant_queryset(
            Document.objects.all(), self.request, organization_field="collection__organization"
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
        return _tenant_queryset(
            Chunk.objects.all(), self.request, organization_field="collection__organization"
        ).select_related("document", "collection")


class SyncRunViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = SyncRunSerializer
    lookup_field = "id"

    def get_queryset(self):
        return _tenant_queryset(
            SyncRun.objects.all(), self.request, organization_field="source__collection__organization"
        ).select_related("source", "source__collection")


class CitationViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = CitationSerializer
    lookup_field = "id"

    def get_queryset(self):
        return _tenant_queryset(
            Citation.objects.all(), self.request, organization_field="job__organization"
        ).select_related("job", "document", "chunk")


class SearchView(APIView):
    """Semantic (pgvector) search across collections stored in Supabase."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [EmbeddingThrottle, BurstThrottle]

    def post(self, request: Request):
        query = request.data.get("query")
        collection_ids = request.data.get("collection_ids", [])
        top_k = int(request.data.get("top_k") or 10)
        min_similarity = request.data.get("min_similarity")

        if not query or not str(query).strip():
            return Response({"detail": "query is required"}, status=status.HTTP_400_BAD_REQUEST)

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
            from apps.knowledge.embeddings import EmbeddingError, embed_texts
            from apps.knowledge.vectorstore import VectorStoreUnavailable, semantic_search

            query_embeddings = embed_texts([str(query).strip()])
            if not query_embeddings:
                message = "Embedding provider returned no vector for the query."
                return Response({"detail": message}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
            threshold = float(min_similarity) if min_similarity not in (None, "") else None
            results = semantic_search(
                query_embeddings[0],
                collection_ids=allowed_ids,
                organization_id=organization_id,
                top_k=top_k,
                min_similarity=threshold,
            )
        except VectorStoreUnavailable as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except EmbeddingError as exc:
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

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    throttle_classes = [EmbeddingThrottle, BurstThrottle]

    def post(self, request: Request):
        query = request.data.get("query")
        collection_ids = request.data.get("collection_ids", [])
        conversation_id = request.data.get("conversation_id")
        include_citations = request.data.get("include_citations", True)

        if not query:
            return Response({"detail": "query is required"}, status=status.HTTP_400_BAD_REQUEST)

        # The job and every collection must belong to the selected tenant.
        organization = organization_for_request(request, required=True)
        collections = Collection.objects.filter(
            id__in=collection_ids, organization=organization, is_active=True
        )

        if not collections.exists():
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
