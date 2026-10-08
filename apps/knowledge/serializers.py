"""Knowledge API serializers (camelCase, matching the frontend contract).

Tenant-owning relations (``organization``, ``collection``, ``createdBy``) are
never writable: they are set from the authorized request context, so a PATCH
cannot move a resource into another tenant.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from django.conf import settings
from drf_spectacular.utils import extend_schema_field, extend_schema_serializer
from rest_framework import serializers

from apps.knowledge.access import normalize_acl
from apps.knowledge.models import Chunk, Citation, Collection, Document, RAGEvaluation, Source, SyncRun
from apps.knowledge.scheduling import parse_sync_schedule

# Backend status → frontend KnowledgeSourceStatus.
SOURCE_STATUS = {
    Source.Status.PENDING: "pending",
    Source.Status.PROCESSING: "indexing",
    Source.Status.INDEXED: "indexed",
    Source.Status.FAILED: "error",
    Source.Status.DELETED: "error",
}
SUPPORTED_SOURCE_TYPES = [choice.value for choice in Source.SourceType]


def _validate_chunking(size: int, overlap: int) -> None:
    if not 100 <= size <= 8000:
        raise serializers.ValidationError({"chunkSize": "chunkSize must be between 100 and 8000 characters."})
    if overlap < 0 or overlap > size // 2:
        raise serializers.ValidationError({"chunkOverlap": "chunkOverlap must be between 0 and chunkSize/2."})


class KnowledgeSourceSerializer(serializers.ModelSerializer):
    collectionId = serializers.UUIDField(source="collection_id", read_only=True)
    type = serializers.CharField(source="source_type", read_only=True)
    status = serializers.SerializerMethodField()
    chunkCount = serializers.IntegerField(source="chunk_count", read_only=True)
    docCount = serializers.IntegerField(source="document_count", read_only=True)
    lastSync = serializers.DateTimeField(source="last_synced_at", read_only=True, allow_null=True)
    lastError = serializers.CharField(source="last_error", read_only=True)
    syncSchedule = serializers.CharField(source="sync_schedule", read_only=True)
    isActive = serializers.BooleanField(source="is_active", read_only=True)
    config = serializers.SerializerMethodField()
    createdBy = serializers.UUIDField(source="created_by_id", read_only=True, allow_null=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Source
        fields = [
            "id",
            "collectionId",
            "type",
            "name",
            "description",
            "status",
            "chunkCount",
            "docCount",
            "lastSync",
            "lastError",
            "syncSchedule",
            "isActive",
            "config",
            "createdBy",
            "createdAt",
            "updatedAt",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.ChoiceField(choices=["pending", "indexing", "indexed", "error"]))
    def get_status(self, obj: Source) -> str:
        return SOURCE_STATUS.get(obj.status, "error")

    @extend_schema_field(serializers.DictField())
    def get_config(self, obj: Source) -> dict:
        """Echo configuration without bulk inline text."""
        config = dict(obj.config or {})
        if isinstance(config.get("text"), str):
            config["textLength"] = len(config.pop("text"))
        return config


class KnowledgeSourceWriteSerializer(serializers.Serializer):
    """Create/update contract for a source; the collection comes from the URL or ``collectionId``."""

    collectionId = serializers.UUIDField(required=False)
    type = serializers.CharField(required=False)
    name = serializers.CharField(max_length=500, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    config = serializers.DictField(required=False)
    syncSchedule = serializers.CharField(required=False, allow_blank=True, max_length=100)
    isActive = serializers.BooleanField(required=False)

    def validate_type(self, value: str) -> str:
        if value not in SUPPORTED_SOURCE_TYPES:
            raise serializers.ValidationError(
                f"Unsupported source type {value!r}; supported types: {', '.join(SUPPORTED_SOURCE_TYPES)}."
            )
        return value

    def validate_syncSchedule(self, value: str) -> str:
        try:
            parse_sync_schedule(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value.strip()

    def validate(self, attrs):
        instance: Source | None = self.instance
        collection: Collection = self.context["collection"]
        user = self.context["request"].user
        if instance is None:
            for required in ("type", "name"):
                if required not in attrs:
                    raise serializers.ValidationError({required: "This field is required."})
        source_type = attrs.get("type", getattr(instance, "source_type", None))
        config = attrs.get("config", getattr(instance, "config", None) or {})
        if instance is not None and "type" in attrs and attrs["type"] != instance.source_type:
            raise serializers.ValidationError({"type": "A source's type cannot be changed."})
        try:
            _visibility, user_ids = normalize_acl(config.get("acl"))
        except ValueError as exc:
            raise serializers.ValidationError({"config": str(exc)}) from exc
        unknown = set(config) - {"text", "url", "asset_id", "acl", "title", "mime_type", "integrationId"}
        if unknown:
            raise serializers.ValidationError(
                {"config": f"Unsupported config keys: {', '.join(sorted(unknown))}."}
            )

        if source_type == Source.SourceType.TEXT:
            text = config.get("text")
            if not isinstance(text, str) or not text.strip():
                raise serializers.ValidationError({"config": "TEXT sources require non-empty config.text."})
            if len(text.encode("utf-8")) > settings.RAG_MAX_EXTRACTED_BYTES:
                raise serializers.ValidationError(
                    {"config": "TEXT source exceeds the extraction byte limit."}
                )
        elif source_type == Source.SourceType.URL:
            parsed = urlsplit(str(config.get("url") or ""))
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise serializers.ValidationError(
                    {"config": "URL sources require an HTTPS URL without embedded credentials."}
                )
        elif source_type == Source.SourceType.INTEGRATION:
            from apps.integrations.models import ConnectorAccount

            if not ConnectorAccount.objects.filter(
                id=config.get("integrationId"),
                organization_id=collection.organization_id,
                status=ConnectorAccount.Status.ACTIVE,
            ).exists():
                raise serializers.ValidationError(
                    {"config": "Integration sources require config.integrationId of a connected integration."}
                )
        elif source_type == Source.SourceType.FILE:
            from apps.assets.access import assets_visible_to
            from apps.assets.models import Asset

            asset_id = config.get("asset_id")
            if not asset_id:
                raise serializers.ValidationError({"config": "FILE sources require config.asset_id."})
            if (
                not assets_visible_to(user, collection.organization_id)
                .filter(id=asset_id, status=Asset.Status.READY)
                .exists()
            ):
                raise serializers.ValidationError({"config": "The selected ready asset was not found."})

        if user_ids:
            from apps.identity.models import User

            member_ids = {
                str(value)
                for value in User.objects.filter(
                    id__in=user_ids, organizations__id=collection.organization_id
                ).values_list("id", flat=True)
            }
            missing = sorted(set(user_ids) - member_ids)
            if missing:
                names = ", ".join(missing)
                raise serializers.ValidationError(
                    {"config": f"ACL users are not members of the collection organization: {names}"}
                )
        attrs["config"] = config
        return attrs

    def create(self, validated_data):
        return Source.objects.create(
            collection=self.context["collection"],
            source_type=validated_data["type"],
            name=validated_data["name"],
            description=validated_data.get("description", ""),
            config=validated_data["config"],
            sync_schedule=validated_data.get("syncSchedule", ""),
            is_active=validated_data.get("isActive", True),
            created_by=self.context["request"].user,
        )

    def update(self, instance: Source, validated_data):
        content_changed = validated_data["config"] != instance.config
        instance.name = validated_data.get("name", instance.name)
        instance.description = validated_data.get("description", instance.description)
        instance.config = validated_data["config"]
        instance.sync_schedule = validated_data.get("syncSchedule", instance.sync_schedule)
        instance.is_active = validated_data.get("isActive", instance.is_active)
        if content_changed:
            instance.status = Source.Status.PENDING
        instance.save()
        return instance


class KnowledgeCollectionSerializer(serializers.ModelSerializer):
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)
    sources = serializers.SerializerMethodField()
    isActive = serializers.BooleanField(source="is_active", required=False)
    documentCount = serializers.IntegerField(source="document_count", read_only=True)
    chunkCount = serializers.IntegerField(source="chunk_count", read_only=True)
    storageSizeBytes = serializers.IntegerField(source="storage_size_bytes", read_only=True)
    lastIndexedAt = serializers.DateTimeField(source="last_indexed_at", read_only=True, allow_null=True)
    embeddingProvider = serializers.CharField(source="embedding_provider", read_only=True)
    embeddingModel = serializers.CharField(source="embedding_model", read_only=True)
    embeddingDimensions = serializers.IntegerField(source="embedding_dimensions", read_only=True)
    chunkSize = serializers.IntegerField(source="chunk_size", required=False)
    chunkOverlap = serializers.IntegerField(source="chunk_overlap", required=False)
    organizationId = serializers.UUIDField(source="organization_id", read_only=True)
    createdBy = serializers.UUIDField(source="created_by_id", read_only=True, allow_null=True)

    class Meta:
        model = Collection
        fields = [
            "id",
            "organizationId",
            "name",
            "description",
            "createdAt",
            "updatedAt",
            "sources",
            "isActive",
            "documentCount",
            "chunkCount",
            "storageSizeBytes",
            "lastIndexedAt",
            "embeddingProvider",
            "embeddingModel",
            "embeddingDimensions",
            "chunkSize",
            "chunkOverlap",
            "metadata",
            "createdBy",
        ]
        read_only_fields = ["id", "organizationId", "createdAt", "updatedAt", "sources", "createdBy"]

    @extend_schema_field(KnowledgeSourceSerializer(many=True))
    def get_sources(self, obj: Collection) -> list[dict]:
        sources = self.context.get("visible_sources", {}).get(obj.id)
        if sources is None:
            sources = list(obj.sources.all())
        return KnowledgeSourceSerializer(sources, many=True).data

    def validate(self, attrs):
        size = attrs.get("chunk_size", getattr(self.instance, "chunk_size", settings.RAG_CHUNK_SIZE))
        overlap = attrs.get(
            "chunk_overlap",
            getattr(self.instance, "chunk_overlap", min(settings.RAG_CHUNK_OVERLAP, size // 2)),
        )
        _validate_chunking(size, overlap)
        return attrs

    def create(self, validated_data):
        from apps.knowledge.embeddings import EmbeddingError, get_embedding_provider

        try:
            provider = get_embedding_provider()
        except EmbeddingError as exc:
            raise serializers.ValidationError({"embedding": str(exc)}) from exc
        validated_data.setdefault("chunk_size", settings.RAG_CHUNK_SIZE)
        validated_data.setdefault(
            "chunk_overlap", min(settings.RAG_CHUNK_OVERLAP, validated_data["chunk_size"] // 2)
        )
        return Collection.objects.create(
            embedding_provider=provider.provider_name,
            embedding_model=provider.model_name,
            embedding_dimensions=settings.VECTOR_EMBEDDING_DIMENSIONS,
            **validated_data,
        )


@extend_schema_serializer(component_name="KnowledgeDocument")
class DocumentSerializer(serializers.ModelSerializer):
    sourceId = serializers.UUIDField(source="source_id", read_only=True)
    sourceName = serializers.CharField(source="source.name", read_only=True)
    collectionId = serializers.UUIDField(source="collection_id", read_only=True)
    mimeType = serializers.CharField(source="mime_type", read_only=True)
    sizeBytes = serializers.IntegerField(source="size_bytes", read_only=True)
    pageCount = serializers.IntegerField(source="page_count", read_only=True)
    chunkCount = serializers.IntegerField(source="chunk_count", read_only=True)
    contentHash = serializers.CharField(source="content_hash", read_only=True)
    lastError = serializers.CharField(source="last_error", read_only=True)
    indexedAt = serializers.DateTimeField(source="indexed_at", read_only=True, allow_null=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Document
        fields = [
            "id",
            "sourceId",
            "sourceName",
            "collectionId",
            "title",
            "mimeType",
            "sizeBytes",
            "pageCount",
            "status",
            "visibility",
            "classification",
            "chunkCount",
            "contentHash",
            "lastError",
            "indexedAt",
            "createdAt",
            "updatedAt",
        ]
        read_only_fields = fields


class ChunkSerializer(serializers.ModelSerializer):
    documentId = serializers.UUIDField(source="document_id", read_only=True)
    documentTitle = serializers.CharField(source="document.title", read_only=True)
    collectionId = serializers.UUIDField(source="collection_id", read_only=True)
    chunkIndex = serializers.IntegerField(source="chunk_index", read_only=True)
    tokenCount = serializers.IntegerField(source="token_count", read_only=True)
    headingPath = serializers.ListField(source="heading_path", child=serializers.CharField(), read_only=True)
    pageNumber = serializers.IntegerField(source="page_number", read_only=True, allow_null=True)
    offsetStart = serializers.IntegerField(source="offset_start", read_only=True)
    offsetEnd = serializers.IntegerField(source="offset_end", read_only=True)
    embeddingVersion = serializers.CharField(source="embedding_version", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = Chunk
        fields = [
            "id",
            "documentId",
            "documentTitle",
            "collectionId",
            "chunkIndex",
            "content",
            "tokenCount",
            "headingPath",
            "pageNumber",
            "offsetStart",
            "offsetEnd",
            "embeddingVersion",
            "createdAt",
        ]
        read_only_fields = fields


class SyncRunSerializer(serializers.ModelSerializer):
    sourceId = serializers.UUIDField(source="source_id", read_only=True)
    sourceName = serializers.CharField(source="source.name", read_only=True)
    documentsProcessed = serializers.IntegerField(source="documents_processed", read_only=True)
    documentsAdded = serializers.IntegerField(source="documents_added", read_only=True)
    documentsUpdated = serializers.IntegerField(source="documents_updated", read_only=True)
    documentsDeleted = serializers.IntegerField(source="documents_deleted", read_only=True)
    chunksCreated = serializers.IntegerField(source="chunks_created", read_only=True)
    chunksDeleted = serializers.IntegerField(source="chunks_deleted", read_only=True)
    errorMessage = serializers.CharField(source="error_message", read_only=True)
    startedAt = serializers.DateTimeField(source="started_at", read_only=True)
    completedAt = serializers.DateTimeField(source="completed_at", read_only=True, allow_null=True)

    class Meta:
        model = SyncRun
        fields = [
            "id",
            "sourceId",
            "sourceName",
            "status",
            "documentsProcessed",
            "documentsAdded",
            "documentsUpdated",
            "documentsDeleted",
            "chunksCreated",
            "chunksDeleted",
            "errorMessage",
            "startedAt",
            "completedAt",
        ]
        read_only_fields = fields


class CitationSerializer(serializers.ModelSerializer):
    jobId = serializers.UUIDField(source="job_id", read_only=True, allow_null=True)
    agentRunId = serializers.UUIDField(source="agent_run_id", read_only=True, allow_null=True)
    chunkId = serializers.UUIDField(source="chunk_id", read_only=True)
    documentId = serializers.UUIDField(source="document_id", read_only=True)
    documentTitle = serializers.CharField(source="document.title", read_only=True)
    pageNumber = serializers.IntegerField(source="chunk.page_number", read_only=True, allow_null=True)
    relevanceScore = serializers.FloatField(source="relevance_score", read_only=True)
    citationIndex = serializers.IntegerField(source="citation_index", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = Citation
        fields = [
            "id",
            "jobId",
            "agentRunId",
            "chunkId",
            "documentId",
            "documentTitle",
            "pageNumber",
            "relevanceScore",
            "citationIndex",
            "snippet",
            "createdAt",
        ]
        read_only_fields = fields


class RAGEvaluationSerializer(serializers.ModelSerializer):
    jobId = serializers.UUIDField(source="job_id", read_only=True, allow_null=True)
    createdBy = serializers.UUIDField(source="created_by_id", read_only=True, allow_null=True)
    expectedChunkIds = serializers.JSONField(source="expected_chunk_ids", read_only=True)
    retrievedChunkIds = serializers.JSONField(source="retrieved_chunk_ids", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = RAGEvaluation
        fields = [
            "id",
            "jobId",
            "createdBy",
            "query",
            "expectedChunkIds",
            "retrievedChunkIds",
            "metrics",
            "passed",
            "evaluator",
            "createdAt",
        ]
        read_only_fields = fields


class DocumentGrantSerializer(serializers.Serializer):
    userId = serializers.UUIDField()


class KnowledgeSearchRequestSerializer(serializers.Serializer):
    query = serializers.CharField(max_length=10_000)
    collectionIds = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list, max_length=100
    )
    topK = serializers.IntegerField(required=False, min_value=1, max_value=100)
    minSimilarity = serializers.FloatField(required=False, min_value=0.0, max_value=1.0)


class KnowledgeSearchQuerySerializer(serializers.Serializer):
    """``GET /knowledge/search/`` query parameters."""

    query = serializers.CharField(max_length=10_000)
    collectionId = serializers.UUIDField(required=False)
    topK = serializers.IntegerField(required=False, min_value=1, max_value=100)


class KnowledgeSearchResultSerializer(serializers.Serializer):
    sourceId = serializers.UUIDField()
    collectionId = serializers.UUIDField()
    text = serializers.CharField()
    score = serializers.FloatField()
    chunkId = serializers.UUIDField()
    documentId = serializers.UUIDField()
    documentTitle = serializers.CharField()
    pageNumber = serializers.IntegerField(allow_null=True)
    headingPath = serializers.ListField(child=serializers.CharField())
    retrievalMethods = serializers.ListField(child=serializers.CharField())


class KnowledgeQueryRequestSerializer(serializers.Serializer):
    query = serializers.CharField(max_length=10_000)
    collectionId = serializers.UUIDField(required=False)
    collectionIds = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list, max_length=100
    )
    topK = serializers.IntegerField(required=False, min_value=1, max_value=50)
    conversationId = serializers.UUIDField(required=False, allow_null=True)


class RAGEvaluationRequestSerializer(serializers.Serializer):
    query = serializers.CharField(max_length=10_000)
    sources = serializers.ListField(
        child=serializers.DictField(), required=False, default=list, max_length=100
    )
    answer = serializers.CharField(allow_blank=True)
    expectedChunkIds = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list, max_length=100
    )


class EmbeddingsRequestSerializer(serializers.Serializer):
    texts = serializers.ListField(
        child=serializers.CharField(max_length=20_000, allow_blank=False), min_length=1, max_length=96
    )
    taskType = serializers.ChoiceField(choices=["document", "query"], required=False, default="document")
