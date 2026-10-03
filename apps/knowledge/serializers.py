from urllib.parse import urlsplit

from django.conf import settings
from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers

from apps.knowledge.access import normalize_acl
from apps.knowledge.models import Chunk, Citation, Collection, Document, RAGEvaluation, Source, SyncRun
from apps.knowledge.scheduling import parse_sync_schedule


class CollectionSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)

    class Meta:
        model = Collection
        fields = [
            "id",
            "organization",
            "organization_name",
            "name",
            "description",
            "embedding_provider",
            "embedding_model",
            "embedding_dimensions",
            "chunk_size",
            "chunk_overlap",
            "metadata",
            "is_active",
            "document_count",
            "chunk_count",
            "storage_size_bytes",
            "last_indexed_at",
            "created_by",
            "created_by_email",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "document_count",
            "chunk_count",
            "storage_size_bytes",
            "last_indexed_at",
            "created_at",
            "updated_at",
        ]


class CollectionCreateSerializer(serializers.ModelSerializer):
    organization = serializers.UUIDField(source="organization_id", read_only=True)

    class Meta:
        model = Collection
        fields = [
            "organization",
            "name",
            "description",
            "embedding_provider",
            "embedding_model",
            "embedding_dimensions",
            "chunk_size",
            "chunk_overlap",
            "metadata",
        ]

    def validate(self, attrs):
        provider = attrs.get("embedding_provider", settings.RAG_EMBEDDING_PROVIDER).lower()
        default_models = {
            "gemini": settings.GEMINI_EMBEDDING_MODEL,
            "echo": "echo-deterministic",
            "openai": settings.RAG_EMBEDDING_MODEL,
        }
        model = attrs.get("embedding_model") or default_models.get(provider, "")
        dimensions = attrs.get("embedding_dimensions", settings.VECTOR_EMBEDDING_DIMENSIONS)
        if provider != settings.RAG_EMBEDDING_PROVIDER.lower():
            raise serializers.ValidationError(
                {"embedding_provider": "Collections must use the server's configured embedding provider."}
            )
        configured_model = (
            settings.GEMINI_EMBEDDING_MODEL if provider == "gemini" else settings.RAG_EMBEDDING_MODEL
        )
        if provider != "echo" and model != configured_model:
            raise serializers.ValidationError(
                {"embedding_model": "Collections must use the server's configured embedding model."}
            )
        if dimensions != settings.VECTOR_EMBEDDING_DIMENSIONS:
            raise serializers.ValidationError(
                {"embedding_dimensions": "Collection dimensions must match the pgvector column width."}
            )
        attrs["embedding_provider"] = provider
        attrs["embedding_model"] = model
        attrs["embedding_dimensions"] = dimensions
        return attrs


class SourceSerializer(serializers.ModelSerializer):
    collection_name = serializers.CharField(source="collection.name", read_only=True)
    created_by_email = serializers.EmailField(source="created_by.email", read_only=True)

    class Meta:
        model = Source
        fields = [
            "id",
            "collection",
            "collection_name",
            "source_type",
            "name",
            "description",
            "config",
            "status",
            "document_count",
            "chunk_count",
            "last_synced_at",
            "last_error",
            "sync_schedule",
            "is_active",
            "created_by",
            "created_by_email",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "document_count",
            "chunk_count",
            "last_synced_at",
            "last_error",
            "created_at",
            "updated_at",
        ]


class SourceCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Source
        fields = ["collection", "source_type", "name", "description", "config", "sync_schedule"]

    def validate_sync_schedule(self, value):
        try:
            parse_sync_schedule(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value.strip()

    def validate(self, attrs):
        source_type = attrs.get("source_type")
        config = attrs.get("config") or {}
        if not isinstance(config, dict):
            raise serializers.ValidationError({"config": "config must be an object."})
        try:
            _visibility, user_ids = normalize_acl(config.get("acl"))
        except ValueError as exc:
            raise serializers.ValidationError({"config": str(exc)}) from exc

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
        elif source_type == Source.SourceType.FILE:
            asset_id = config.get("asset_id")
            if not asset_id:
                raise serializers.ValidationError({"config": "FILE sources require config.asset_id."})
            from apps.assets.models import Asset

            collection = attrs.get("collection")
            if not Asset.objects.filter(
                id=asset_id,
                organization_id=collection.organization_id,
                status=Asset.Status.READY,
            ).exists():
                raise serializers.ValidationError({"config": "The selected ready asset was not found."})

        if user_ids:
            from apps.identity.models import User

            collection = attrs.get("collection")
            member_ids = {
                str(value)
                for value in User.objects.filter(
                    id__in=user_ids, organizations__id=collection.organization_id
                ).values_list("id", flat=True)
            }
            missing = sorted(set(user_ids) - member_ids)
            if missing:
                missing_users = ", ".join(missing)
                raise serializers.ValidationError(
                    {"config": f"ACL users are not members of the collection organization: {missing_users}"}
                )
        return attrs


@extend_schema_serializer(component_name="KnowledgeDocument")
class DocumentSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source="source.name", read_only=True)
    collection_name = serializers.CharField(source="collection.name", read_only=True)

    class Meta:
        model = Document
        fields = [
            "id",
            "source",
            "source_name",
            "collection",
            "collection_name",
            "external_id",
            "title",
            "content_hash",
            "mime_type",
            "size_bytes",
            "language",
            "page_count",
            "status",
            "metadata",
            "acl",
            "visibility",
            "classification",
            "chunk_count",
            "vector_ids",
            "last_error",
            "indexed_at",
            "deleted_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "content_hash",
            "status",
            "chunk_count",
            "vector_ids",
            "indexed_at",
            "deleted_at",
            "created_at",
            "updated_at",
        ]


class ChunkSerializer(serializers.ModelSerializer):
    document_title = serializers.CharField(source="document.title", read_only=True)
    collection_name = serializers.CharField(source="collection.name", read_only=True)

    class Meta:
        model = Chunk
        fields = [
            "id",
            "document",
            "document_title",
            "collection",
            "collection_name",
            "chunk_index",
            "content",
            "token_count",
            "heading_path",
            "page_number",
            "offset_start",
            "offset_end",
            "vector_id",
            "metadata",
            "acl",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class SyncRunSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source="source.name", read_only=True)

    class Meta:
        model = SyncRun
        fields = [
            "id",
            "source",
            "source_name",
            "status",
            "documents_processed",
            "documents_added",
            "documents_updated",
            "documents_deleted",
            "chunks_created",
            "chunks_updated",
            "chunks_deleted",
            "error_message",
            "started_at",
            "completed_at",
        ]
        read_only_fields = ["id", "started_at", "completed_at"]


class CitationSerializer(serializers.ModelSerializer):
    document_title = serializers.CharField(source="document.title", read_only=True)
    chunk_content = serializers.CharField(source="chunk.content", read_only=True)

    class Meta:
        model = Citation
        fields = [
            "id",
            "job",
            "chunk",
            "document",
            "document_title",
            "chunk_content",
            "relevance_score",
            "citation_index",
            "snippet",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class RAGEvaluationSerializer(serializers.ModelSerializer):
    class Meta:
        model = RAGEvaluation
        fields = [
            "id",
            "job",
            "created_by",
            "query",
            "expected_chunk_ids",
            "retrieved_chunk_ids",
            "metrics",
            "passed",
            "evaluator",
            "created_at",
        ]
        read_only_fields = fields


class KnowledgeSearchRequestSerializer(serializers.Serializer):
    query = serializers.CharField(max_length=10_000)
    collection_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list, max_length=100
    )
    top_k = serializers.IntegerField(required=False, default=10, min_value=1, max_value=100)
    min_similarity = serializers.FloatField(required=False, min_value=0.0, max_value=1.0)


class RAGQueryRequestSerializer(serializers.Serializer):
    query = serializers.CharField(max_length=10_000)
    collection_ids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=100)
    conversation_id = serializers.UUIDField(required=False, allow_null=True)
    include_citations = serializers.BooleanField(required=False, default=True)


class RAGEvaluationRequestSerializer(serializers.Serializer):
    query = serializers.CharField(max_length=10_000)
    sources = serializers.ListField(
        child=serializers.DictField(), required=False, default=list, max_length=100
    )
    answer = serializers.CharField(allow_blank=True)
    expected_chunk_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list, max_length=100
    )
