from rest_framework import serializers
from apps.knowledge.models import Collection, Source, Document, Chunk, SyncRun, Citation


class CollectionSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)

    class Meta:
        model = Collection
        fields = [
            'id', 'organization', 'organization_name', 'name', 'description',
            'embedding_provider', 'embedding_model', 'embedding_dimensions',
            'chunk_size', 'chunk_overlap', 'metadata', 'is_active',
            'document_count', 'chunk_count', 'storage_size_bytes',
            'last_indexed_at', 'created_by', 'created_by_email',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'document_count', 'chunk_count', 'storage_size_bytes',
            'last_indexed_at', 'created_at', 'updated_at'
        ]


class CollectionCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Collection
        fields = [
            'name', 'description', 'embedding_provider', 'embedding_model',
            'embedding_dimensions', 'chunk_size', 'chunk_overlap', 'metadata'
        ]


class SourceSerializer(serializers.ModelSerializer):
    collection_name = serializers.CharField(source='collection.name', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)

    class Meta:
        model = Source
        fields = [
            'id', 'collection', 'collection_name', 'source_type', 'name',
            'description', 'config', 'status', 'document_count',
            'chunk_count', 'last_synced_at', 'last_error', 'sync_schedule',
            'is_active', 'created_by', 'created_by_email', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'status', 'document_count', 'chunk_count',
            'last_synced_at', 'last_error', 'created_at', 'updated_at'
        ]


class SourceCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Source
        fields = [
            'collection', 'source_type', 'name', 'description', 'config', 'sync_schedule'
        ]


class DocumentSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source='source.name', read_only=True)
    collection_name = serializers.CharField(source='collection.name', read_only=True)

    class Meta:
        model = Document
        fields = [
            'id', 'source', 'source_name', 'collection', 'collection_name',
            'external_id', 'title', 'content_hash', 'mime_type', 'size_bytes',
            'language', 'page_count', 'status', 'metadata', 'acl',
            'classification', 'chunk_count', 'vector_ids', 'last_error',
            'indexed_at', 'deleted_at', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'content_hash', 'status', 'chunk_count', 'vector_ids',
            'indexed_at', 'deleted_at', 'created_at', 'updated_at'
        ]


class ChunkSerializer(serializers.ModelSerializer):
    document_title = serializers.CharField(source='document.title', read_only=True)
    collection_name = serializers.CharField(source='collection.name', read_only=True)

    class Meta:
        model = Chunk
        fields = [
            'id', 'document', 'document_title', 'collection', 'collection_name',
            'chunk_index', 'content', 'token_count', 'heading_path',
            'page_number', 'offset_start', 'offset_end', 'vector_id',
            'metadata', 'acl', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class SyncRunSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source='source.name', read_only=True)

    class Meta:
        model = SyncRun
        fields = [
            'id', 'source', 'source_name', 'status',
            'documents_processed', 'documents_added', 'documents_updated',
            'documents_deleted', 'chunks_created', 'chunks_updated',
            'chunks_deleted', 'error_message', 'started_at', 'completed_at'
        ]
        read_only_fields = ['id', 'started_at', 'completed_at']


class CitationSerializer(serializers.ModelSerializer):
    document_title = serializers.CharField(source='document.title', read_only=True)
    chunk_content = serializers.CharField(source='chunk.content', read_only=True)

    class Meta:
        model = Citation
        fields = [
            'id', 'job', 'chunk', 'document', 'document_title', 'chunk_content',
            'relevance_score', 'citation_index', 'snippet', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']