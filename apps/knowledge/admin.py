from django.contrib import admin
from apps.knowledge.models import Collection, Source, Document, Chunk, SyncRun, Citation


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'organization', 'embedding_provider', 'embedding_model', 'document_count', 'chunk_count', 'is_active', 'created_at')
    list_filter = ('embedding_provider', 'is_active', 'created_at')
    search_fields = ('name', 'organization__name')
    readonly_fields = ('id', 'document_count', 'chunk_count', 'storage_size_bytes', 'last_indexed_at', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('organization', 'created_by')


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'collection', 'source_type', 'status', 'document_count', 'is_active', 'created_at')
    list_filter = ('source_type', 'status', 'is_active', 'created_at')
    search_fields = ('name', 'collection__name')
    readonly_fields = ('id', 'document_count', 'chunk_count', 'last_synced_at', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('collection', 'created_by')


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'source', 'status', 'mime_type', 'size_bytes', 'chunk_count', 'created_at')
    list_filter = ('status', 'mime_type', 'classification', 'created_at')
    search_fields = ('title', 'source__name', 'external_id', 'content_hash')
    readonly_fields = ('id', 'content_hash', 'chunk_count', 'vector_ids', 'indexed_at', 'deleted_at', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('source', 'collection')


@admin.register(Chunk)
class ChunkAdmin(admin.ModelAdmin):
    list_display = ('id', 'document', 'chunk_index', 'token_count', 'vector_id', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('document__title', 'content', 'vector_id')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('document', 'chunk_index')
    raw_id_fields = ('document', 'collection')


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ('id', 'source', 'status', 'documents_processed', 'chunks_created', 'started_at', 'completed_at')
    list_filter = ('status', 'started_at')
    search_fields = ('source__name',)
    readonly_fields = ('id', 'started_at', 'completed_at')
    ordering = ('-started_at',)
    raw_id_fields = ('source',)


@admin.register(Citation)
class CitationAdmin(admin.ModelAdmin):
    list_display = ('id', 'job', 'document', 'citation_index', 'relevance_score', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('job__id', 'document__title')
    readonly_fields = ('id', 'created_at')
    ordering = ('job', 'citation_index')
    raw_id_fields = ('job', 'chunk', 'document')