"""Knowledge (Agentic RAG) API under ``/api/v1/knowledge/``.

Every queryset is scoped to the one organization selected for the request and
then to the document ACL. Writes that change a source's content or access are
limited to the source's creator or an organization admin; tenant-owning
relations are never client-writable.
"""

from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.db.models import CharField, Prefetch, Q, QuerySet, Value
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models.functions import Coalesce
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.throttling import BurstThrottle, EmbeddingThrottle
from apps.core.views import APIView
from apps.events.outbox import enqueue_outbox_event
from apps.identity.authorization import (
    HasOrganizationWriteAccess,
    organization_for_request,
    require_organization_write_access,
    tenant_scoped_queryset,
    user_can_edit_organization,
    user_has_role,
)
from apps.identity.models import Role
from apps.knowledge.access import accessible_chunks, accessible_documents
from apps.knowledge.models import (
    Chunk,
    Citation,
    Collection,
    Document,
    DocumentAccessGrant,
    RAGEvaluation,
    Source,
)
from apps.knowledge.serializers import (
    ChunkSerializer,
    CitationSerializer,
    DocumentGrantSerializer,
    DocumentSerializer,
    KnowledgeCollectionSerializer,
    KnowledgeQueryRequestSerializer,
    KnowledgeSearchQuerySerializer,
    KnowledgeSearchRequestSerializer,
    KnowledgeSearchResultSerializer,
    KnowledgeSourceSerializer,
    KnowledgeSourceWriteSerializer,
    RAGEvaluationRequestSerializer,
    RAGEvaluationSerializer,
    SyncRunSerializer,
)


def _organization(view) -> object | None:
    if getattr(view, "swagger_fake_view", False):
        return None
    return organization_for_request(view.request, required=True)


def _is_admin(user, organization_id) -> bool:
    return user_has_role(user, Role.RoleType.ADMIN, organization_id)


def visible_sources(user, organization) -> QuerySet[Source]:
    """Sources in the organization the user may see under each source's ACL."""
    queryset = Source.objects.filter(collection__organization=organization)
    if _is_admin(user, organization.id):
        return queryset
    # Sources without an ACL are organization-visible (COALESCE handles the missing key).
    acl_visibility = Coalesce(
        KeyTextTransform("visibility", KeyTransform("acl", "config")),
        Value("organization"),
        output_field=CharField(),
    )
    return queryset.alias(acl_visibility=acl_visibility).filter(
        Q(created_by=user)
        | Q(acl_visibility="organization")
        | Q(config__acl__user_ids__contains=[str(user.id)])
    )


def _require_source_manager(user, source: Source) -> None:
    organization_id = source.collection.organization_id
    require_organization_write_access(user, organization_id)
    if source.created_by_id != user.id and not _is_admin(user, organization_id):
        raise PermissionDenied("Only the source creator or an organization admin may change this source.")


def _queue_sync(source: Source) -> None:
    from apps.knowledge.tasks import sync_source

    sync_source.delay(str(source.id))


def _delete_source(source: Source, actor) -> None:
    with transaction.atomic():
        enqueue_outbox_event(
            topic="knowledge.source.deleted",
            event_key=str(source.id),
            payload={
                "source_id": str(source.id),
                "collection_id": str(source.collection_id),
                "organization_id": str(source.collection.organization_id),
                "actor_id": str(actor.id),
            },
        )
        collection = source.collection
        source.delete()
    from apps.knowledge.tasks import _update_collection_counts

    _update_collection_counts(collection)


class CollectionViewSet(viewsets.ModelViewSet):
    """Collections with their visible sources (unpaginated, per the frontend contract)."""

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = KnowledgeCollectionSerializer
    lookup_field = "id"
    pagination_class = None

    def get_queryset(self):
        organization = _organization(self)
        if organization is None:
            return Collection.objects.none()
        sources = visible_sources(self.request.user, organization).order_by("created_at")
        return (
            Collection.objects.filter(organization=organization)
            .prefetch_related(Prefetch("sources", queryset=sources))
            .order_by("-created_at")[:500]
            if self.action == "list"
            else Collection.objects.filter(organization=organization).prefetch_related(
                Prefetch("sources", queryset=sources)
            )
        )

    def perform_create(self, serializer):
        organization = organization_for_request(self.request, required=True)
        serializer.save(organization=organization, created_by=self.request.user)

    def perform_destroy(self, instance):
        with transaction.atomic():
            enqueue_outbox_event(
                topic="knowledge.collection.deleted",
                event_key=str(instance.id),
                payload={
                    "collection_id": str(instance.id),
                    "organization_id": str(instance.organization_id),
                    "actor_id": str(self.request.user.id),
                },
            )
            instance.delete()

    def _collection_response(self, collection: Collection, *, status_code=status.HTTP_200_OK) -> Response:
        collection = self.get_queryset().get(id=collection.id)
        return Response(self.get_serializer(collection).data, status=status_code)

    @extend_schema(
        request=None, responses={202: inline_serializer("SyncQueued", {"queued": serializers.IntegerField()})}
    )
    @action(detail=True, methods=["post"])
    def sync(self, request: Request, id=None):
        collection = self.get_object()
        sources = visible_sources(request.user, collection.organization).filter(
            collection=collection, is_active=True
        )
        count = 0
        for source in sources:
            _queue_sync(source)
            count += 1
        return Response({"queued": count}, status=status.HTTP_202_ACCEPTED)

    @extend_schema(methods=["GET"], request=None, responses={200: KnowledgeSourceSerializer(many=True)})
    @extend_schema(
        methods=["POST"],
        request=KnowledgeSourceWriteSerializer,
        responses={201: KnowledgeCollectionSerializer},
    )
    @action(detail=True, methods=["get", "post"], url_path="sources")
    def sources(self, request: Request, id=None):
        collection = self.get_object()
        if request.method == "GET":
            sources = visible_sources(request.user, collection.organization).filter(collection=collection)
            return Response(KnowledgeSourceSerializer(sources.order_by("created_at"), many=True).data)
        serializer = KnowledgeSourceWriteSerializer(
            data=request.data, context={"request": request, "collection": collection}
        )
        serializer.is_valid(raise_exception=True)
        source = serializer.save()
        _queue_sync(source)
        return self._collection_response(collection, status_code=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses={200: KnowledgeCollectionSerializer})
    @action(detail=True, methods=["delete"], url_path=r"sources/(?P<source_id>[0-9a-f-]+)")
    def remove_source(self, request: Request, id=None, source_id=None):
        collection = self.get_object()
        source = (
            visible_sources(request.user, collection.organization)
            .filter(collection=collection, id=source_id)
            .select_related("collection")
            .first()
        )
        if source is None:
            raise NotFound("Source not found.")
        _require_source_manager(request.user, source)
        _delete_source(source, request.user)
        return self._collection_response(collection)


class SourceViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    lookup_field = "id"

    def get_queryset(self):
        organization = _organization(self)
        if organization is None:
            return Source.objects.none()
        return (
            visible_sources(self.request.user, organization)
            .select_related("collection", "collection__organization", "created_by")
            .order_by("-created_at")
        )

    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return KnowledgeSourceWriteSerializer
        return KnowledgeSourceSerializer

    @extend_schema(request=KnowledgeSourceWriteSerializer, responses={201: KnowledgeSourceSerializer})
    def create(self, request: Request, *args, **kwargs):
        organization = organization_for_request(request, required=True)
        collection = Collection.objects.filter(
            organization=organization, id=request.data.get("collectionId")
        ).first()
        if collection is None:
            raise NotFound("Collection not found in the selected organization.")
        serializer = KnowledgeSourceWriteSerializer(
            data=request.data, context={"request": request, "collection": collection}
        )
        serializer.is_valid(raise_exception=True)
        source = serializer.save()
        _queue_sync(source)
        return Response(KnowledgeSourceSerializer(source).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=KnowledgeSourceWriteSerializer, responses={200: KnowledgeSourceSerializer})
    def update(self, request: Request, *args, **kwargs):
        source = self.get_object()
        _require_source_manager(request.user, source)
        serializer = KnowledgeSourceWriteSerializer(
            source,
            data=request.data,
            partial=True,  # PUT and PATCH both merge into the existing source
            context={"request": request, "collection": source.collection},
        )
        serializer.is_valid(raise_exception=True)
        source = serializer.save()
        if source.status == Source.Status.PENDING and source.is_active:
            _queue_sync(source)
        return Response(KnowledgeSourceSerializer(source).data)

    def perform_destroy(self, instance):
        _require_source_manager(self.request.user, instance)
        _delete_source(instance, self.request.user)

    @extend_schema(request=None, responses={202: KnowledgeSourceSerializer})
    @action(detail=True, methods=["post"])
    def sync(self, request: Request, id=None):
        source = self.get_object()
        if not source.is_active:
            return Response({"detail": "The source is inactive."}, status=status.HTTP_409_CONFLICT)
        _queue_sync(source)
        source.refresh_from_db()
        return Response(KnowledgeSourceSerializer(source).data, status=status.HTTP_202_ACCEPTED)

    @extend_schema(request=None, responses={200: SyncRunSerializer(many=True)})
    @action(detail=True, methods=["get"], url_path="sync-runs")
    def sync_runs(self, request: Request, id=None):
        source = self.get_object()
        runs = source.sync_runs.select_related("source").order_by("-started_at")
        page = self.paginate_queryset(runs)
        if page is not None:
            return self.get_paginated_response(SyncRunSerializer(page, many=True).data)
        return Response(SyncRunSerializer(runs, many=True).data)


class DocumentViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet
):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = DocumentSerializer
    lookup_field = "id"

    def get_queryset(self):
        organization = _organization(self)
        if organization is None:
            return Document.objects.none()
        queryset = Document.objects.filter(collection__organization=organization).exclude(
            status=Document.Status.DELETED
        )
        if collection_id := self.request.query_params.get("collectionId"):
            queryset = queryset.filter(collection_id=collection_id)
        if source_id := self.request.query_params.get("sourceId"):
            queryset = queryset.filter(source_id=source_id)
        return (
            accessible_documents(queryset, self.request.user, organization_id=organization.id)
            .select_related("source", "source__collection", "collection")
            .order_by("-created_at")
        )

    def perform_destroy(self, instance):
        from apps.knowledge.tasks import soft_delete_document

        _require_source_manager(self.request.user, instance.source)
        soft_delete_document(instance, actor=self.request.user)

    @extend_schema(request=None, responses={200: ChunkSerializer(many=True)})
    @action(detail=True, methods=["get"])
    def chunks(self, request: Request, id=None):
        document = self.get_object()
        chunks = document.chunks.select_related("document").order_by("chunk_index")
        page = self.paginate_queryset(chunks)
        if page is not None:
            return self.get_paginated_response(ChunkSerializer(page, many=True).data)
        return Response(ChunkSerializer(chunks, many=True).data)

    @extend_schema(
        request=None,
        responses={202: inline_serializer("ReindexQueued", {"documentId": serializers.UUIDField()})},
    )
    @action(detail=True, methods=["post"])
    def reindex(self, request: Request, id=None):
        """Re-run extraction, chunking and embedding; the old vectors stay live until replaced."""
        from apps.knowledge.tasks import process_document

        document = self.get_object()
        _require_source_manager(request.user, document.source)
        Document.objects.filter(id=document.id).update(content_hash="reindex", index_attempts=0)
        process_document.delay(str(document.id))
        return Response({"documentId": str(document.id)}, status=status.HTTP_202_ACCEPTED)

    @extend_schema(methods=["GET"], request=None, responses={200: DocumentGrantSerializer(many=True)})
    @extend_schema(
        methods=["POST"], request=DocumentGrantSerializer, responses={201: DocumentGrantSerializer}
    )
    @action(detail=True, methods=["get", "post"])
    def grants(self, request: Request, id=None):
        """Explicit principals allowed to read a restricted document."""
        document = self.get_object()
        _require_source_manager(request.user, document.source)
        if request.method == "GET":
            user_ids = DocumentAccessGrant.objects.filter(document=document).values_list("user_id", flat=True)
            return Response([{"userId": str(value)} for value in user_ids])
        serializer = DocumentGrantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_id = serializer.validated_data["userId"]
        from apps.identity.models import User

        if not User.objects.filter(
            id=user_id, organizations__id=document.collection.organization_id
        ).exists():
            raise serializers.ValidationError({"userId": "The user is not a member of this organization."})
        DocumentAccessGrant.objects.get_or_create(
            document=document, user_id=user_id, defaults={"granted_by": request.user}
        )
        return Response({"userId": str(user_id)}, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses={204: None})
    @action(detail=True, methods=["delete"], url_path=r"grants/(?P<user_id>[0-9a-f-]+)")
    def revoke_grant(self, request: Request, id=None, user_id=None):
        document = self.get_object()
        _require_source_manager(request.user, document.source)
        DocumentAccessGrant.objects.filter(document=document, user_id=user_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        request=None,
        responses={
            200: inline_serializer(
                "KnowledgeDocumentDownload",
                {"url": serializers.URLField(), "expiresIn": serializers.IntegerField()},
            )
        },
    )
    @action(detail=True, methods=["post"])
    def download(self, request: Request, id=None):
        """Short-lived link to a FILE document's original bytes, under the document ACL."""
        from apps.assets.imagekit import generate_signed_delivery_url, imagekit_is_configured
        from apps.assets.models import Asset

        document = self.get_object()
        source = document.source
        asset_id = (source.config or {}).get("asset_id")
        if source.source_type != Source.SourceType.FILE or not asset_id:
            raise NotFound("This document has no downloadable original file.")
        asset = Asset.objects.filter(
            id=asset_id, organization_id=document.collection.organization_id, status=Asset.Status.READY
        ).first()
        if asset is None:
            raise NotFound("The original file is no longer available.")
        if not imagekit_is_configured():
            return Response(
                {"detail": "ImageKit is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        return Response(
            {
                "url": generate_signed_delivery_url(asset.imagekit_file_path),
                "expiresIn": settings.IMAGEKIT_SIGNED_URL_TTL_SECONDS,
            }
        )


class ChunkViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ChunkSerializer
    lookup_field = "id"

    def get_queryset(self):
        organization = _organization(self)
        if organization is None:
            return Chunk.objects.none()
        queryset = Chunk.objects.filter(collection__organization=organization).exclude(
            document__status=Document.Status.DELETED
        )
        return (
            accessible_chunks(queryset, self.request.user, organization_id=organization.id)
            .select_related("document", "collection")
            .order_by("document_id", "chunk_index")
        )


class SyncRunViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = SyncRunSerializer
    lookup_field = "id"

    def get_queryset(self):
        from apps.knowledge.models import SyncRun

        organization = _organization(self)
        if organization is None:
            return SyncRun.objects.none()
        return (
            SyncRun.objects.filter(source__in=visible_sources(self.request.user, organization))
            .select_related("source")
            .order_by("-started_at")
        )


class CitationViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = CitationSerializer
    lookup_field = "id"

    @extend_schema(
        parameters=[
            OpenApiParameter("jobId", str, required=False),
            OpenApiParameter("agentRunId", str, required=False),
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        organization = _organization(self)
        if organization is None:
            return Citation.objects.none()
        queryset = Citation.objects.filter(document__collection__organization=organization)
        if not user_can_edit_organization(self.request.user, organization.id):
            # Viewers see evidence only for their own jobs and agent runs.
            queryset = queryset.filter(Q(job__owner=self.request.user) | Q(agent_run__user=self.request.user))
        if job_id := self.request.query_params.get("jobId"):
            queryset = queryset.filter(job_id=job_id)
        if run_id := self.request.query_params.get("agentRunId"):
            queryset = queryset.filter(agent_run_id=run_id)
        documents = accessible_documents(
            Document.objects.all(), self.request.user, organization_id=organization.id
        )
        return (
            queryset.filter(document__in=documents)
            .select_related("document", "chunk")
            .order_by("job_id", "agent_run_id", "citation_index")
        )


def _authorized_collection_ids(request: Request, requested: list) -> tuple[object, list]:
    organization = organization_for_request(request, required=True)
    collections = Collection.objects.filter(organization=organization, is_active=True)
    if requested:
        collections = collections.filter(id__in=requested)
    return organization, list(collections.values_list("id", flat=True))


def _search(request: Request, *, query: str, requested: list, top_k: int | None, min_similarity=None):
    import uuid

    from apps.usage.models import Feature
    from apps.usage.services import metered

    organization, allowed_ids = _authorized_collection_ids(request, requested)
    if requested and len(allowed_ids) != len(set(requested)):
        raise NotFound("One or more collections were not found in the selected organization.")
    if not allowed_ids:
        return organization, allowed_ids, None
    with metered(
        organization=organization,
        user=request.user,
        feature=Feature.SEARCH_QUERIES,
        source_type="knowledge_search",
        source_id=uuid.uuid4(),
    ):
        retrieval = _run_search(request, query, allowed_ids, organization, top_k, min_similarity)
    return organization, allowed_ids, retrieval


def _run_search(request, query, allowed_ids, organization, top_k, min_similarity):
    from apps.knowledge.retrieval import embed_query_or_none, hybrid_retrieve

    query_vector, _reason = embed_query_or_none(query)
    return hybrid_retrieve(
        query,
        query_vector,
        collection_ids=allowed_ids,
        organization_id=organization.id,
        user=request.user,
        top_k=top_k or settings.RAG_TOP_K,
        min_similarity=min_similarity,
        trace_id=getattr(request, "trace_id", "") or "",
    )


def _search_result(item: dict) -> dict:
    return {
        "sourceId": item.get("source_id"),
        "collectionId": item.get("collection_id"),
        "text": item.get("content", ""),
        "score": item.get("rerank_score", item.get("score", 0.0)),
        "chunkId": item.get("chunk_id"),
        "documentId": item.get("document_id"),
        "documentTitle": item.get("document_title", ""),
        "pageNumber": item.get("page_number"),
        "headingPath": item.get("heading_path") or [],
        "retrievalMethods": item.get("retrieval_methods") or [],
    }


class SearchView(APIView):
    """Hybrid (pgvector + PostgreSQL full-text) search with reranking."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [EmbeddingThrottle, BurstThrottle]

    @extend_schema(
        parameters=[KnowledgeSearchQuerySerializer],
        responses={200: KnowledgeSearchResultSerializer(many=True)},
    )
    def get(self, request: Request):
        params = KnowledgeSearchQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        collection_id = params.validated_data.get("collectionId")
        _org, _ids, retrieval = _search(
            request,
            query=params.validated_data["query"].strip(),
            requested=[collection_id] if collection_id else [],
            top_k=params.validated_data.get("topK"),
        )
        results = [_search_result(item) for item in (retrieval.results if retrieval else [])]
        response = Response(results)
        if retrieval is not None:
            response["X-Retrieval-Reranker"] = retrieval.reranker
            response["X-Retrieval-Degraded"] = ",".join(retrieval.degraded)
        return response

    @extend_schema(
        request=KnowledgeSearchRequestSerializer,
        responses={
            200: inline_serializer(
                "KnowledgeSearchResponse",
                {
                    "query": serializers.CharField(),
                    "collectionIds": serializers.ListField(child=serializers.UUIDField()),
                    "results": KnowledgeSearchResultSerializer(many=True),
                    "resultCount": serializers.IntegerField(),
                    "reranker": serializers.CharField(),
                    "degraded": serializers.ListField(child=serializers.CharField()),
                },
            )
        },
    )
    def post(self, request: Request):
        serializer = KnowledgeSearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        query = serializer.validated_data["query"].strip()
        _org, allowed_ids, retrieval = _search(
            request,
            query=query,
            requested=serializer.validated_data["collectionIds"],
            top_k=serializer.validated_data.get("topK"),
            min_similarity=serializer.validated_data.get("minSimilarity"),
        )
        results = [_search_result(item) for item in (retrieval.results if retrieval else [])]
        return Response(
            {
                "query": query,
                "collectionIds": [str(value) for value in allowed_ids],
                "results": results,
                "resultCount": len(results),
                "reranker": retrieval.reranker if retrieval else "",
                "degraded": retrieval.degraded if retrieval else [],
            }
        )


def _create_rag_job(request: Request, payload: dict):
    from apps.jobs.models import Job
    from apps.jobs.services import reserve_job_credits

    serializer = KnowledgeQueryRequestSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    requested = list(
        dict.fromkeys([*data["collectionIds"], *([data["collectionId"]] if data.get("collectionId") else [])])
    )
    organization, allowed_ids = _authorized_collection_ids(request, requested)
    if requested and len(allowed_ids) != len(requested):
        raise NotFound("One or more collections were not found in the selected organization.")
    if not allowed_ids:
        raise NotFound("The organization has no active knowledge collections.")
    if conversation_id := data.get("conversationId"):
        from apps.conversations.models import Conversation

        if not tenant_scoped_queryset(
            Conversation.objects.filter(id=conversation_id), request.user, organization_id=organization.id
        ).exists():
            raise NotFound("Conversation not found.")
    with transaction.atomic():
        job = Job.objects.create(
            owner=request.user,
            organization=organization,
            task_type=Job.TaskType.RAG_QUERY,
            trace_id=getattr(request, "trace_id", "") or "",
            input_payload={
                "query": data["query"].strip(),
                "collection_ids": [str(value) for value in allowed_ids],
                "conversation_id": str(conversation_id) if conversation_id else None,
                "top_k": data.get("topK"),
            },
        )
        reserve_job_credits(job, request.user)
    return job


class KnowledgeQueryView(APIView):
    """Synchronous grounded answer (runs the RAG job inline and returns its result)."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [EmbeddingThrottle, BurstThrottle]

    @extend_schema(
        request=KnowledgeQueryRequestSerializer,
        responses={
            200: inline_serializer(
                "KnowledgeQueryResponse",
                {
                    "answer": serializers.CharField(),
                    "jobId": serializers.UUIDField(),
                    "grounded": serializers.BooleanField(),
                    "sources": serializers.ListField(child=serializers.DictField()),
                    "evaluation": serializers.DictField(),
                    "retrieval": serializers.DictField(),
                },
            )
        },
    )
    def post(self, request: Request):
        from apps.jobs.executor import execute_job
        from apps.jobs.models import Job

        job = _create_rag_job(request, request.data)
        execute_job(job)
        job.refresh_from_db()
        if job.status != Job.Status.COMPLETED:
            return Response(
                {
                    "detail": job.error_message or "The knowledge query failed.",
                    "code": job.error_code,
                    "jobId": str(job.id),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )
        result = job.result or {}
        return Response(
            {
                "answer": result.get("answer", ""),
                "jobId": str(job.id),
                "grounded": bool(result.get("grounded")),
                "sources": result.get("sources", []),
                "evaluation": result.get("evaluation", {}),
                "retrieval": result.get("retrieval", {}),
            }
        )


class RAGQueryView(APIView):
    """Asynchronous grounded answer: queues a ``RAG_QUERY`` job (poll ``/jobs/{id}/``)."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [EmbeddingThrottle, BurstThrottle]

    @extend_schema(
        request=KnowledgeQueryRequestSerializer,
        responses={
            202: inline_serializer(
                "RAGQueryQueued",
                {
                    "jobId": serializers.UUIDField(),
                    "requestId": serializers.UUIDField(),
                    "status": serializers.CharField(),
                },
            )
        },
    )
    def post(self, request: Request):
        from apps.jobs.dispatch import enqueue_job

        job = _create_rag_job(request, request.data)
        enqueue_job(job)
        return Response(
            {"jobId": str(job.id), "requestId": str(job.request_id), "status": "queued"},
            status=status.HTTP_202_ACCEPTED,
        )


class RAGEvaluationView(APIView):
    """Evaluate an authorized retrieval result against an expected evidence set."""

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]

    @extend_schema(
        request=RAGEvaluationRequestSerializer,
        responses={
            201: inline_serializer(
                "RAGEvaluationResult", {"id": serializers.UUIDField(), "passed": serializers.BooleanField()}
            )
        },
    )
    def post(self, request: Request):
        serializer = RAGEvaluationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization = organization_for_request(request, required=True)
        sources = serializer.validated_data["sources"]
        supplied_ids = [item.get("chunkId") or item.get("chunk_id") for item in sources]
        allowed = {
            str(chunk.id): chunk
            for chunk in accessible_chunks(
                Chunk.objects.filter(
                    id__in=[value for value in supplied_ids if value],
                    document__status=Document.Status.INDEXED,
                    collection__is_active=True,
                    document__source__is_active=True,
                ).select_related("document"),
                request.user,
                organization_id=organization.id,
            )
        }
        safe_sources = []
        for index, supplied in enumerate(supplied_ids, start=1):
            chunk = allowed.get(str(supplied))
            if chunk is None:
                continue
            safe_sources.append(
                {
                    "chunk_id": str(chunk.id),
                    "document_id": str(chunk.document_id),
                    "document_title": chunk.document.title,
                    "collection_id": str(chunk.collection_id),
                    "chunk_index": chunk.chunk_index,
                    "citation_index": index,
                    "content": chunk.content,
                }
            )
        from apps.knowledge.retrieval import evaluate_response

        evaluation = evaluate_response(
            organization=organization,
            query=serializer.validated_data["query"].strip(),
            sources=safe_sources,
            answer=serializer.validated_data["answer"],
            expected_chunk_ids=[str(value) for value in serializer.validated_data["expectedChunkIds"]],
            user=request.user,
        )
        return Response(evaluation, status=status.HTTP_201_CREATED)


class RAGEvaluationViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = RAGEvaluationSerializer
    lookup_field = "id"

    def get_queryset(self):
        organization = _organization(self)
        if organization is None:
            return RAGEvaluation.objects.none()
        queryset = RAGEvaluation.objects.filter(organization=organization).order_by("-created_at")
        if user_can_edit_organization(self.request.user, organization.id):
            return queryset
        return queryset.filter(created_by=self.request.user)
