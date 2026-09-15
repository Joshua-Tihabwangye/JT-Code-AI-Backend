from __future__ import annotations

import hashlib
import uuid

from celery import shared_task
from django.conf import settings
from django.utils import timezone


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _collection_chunking(collection) -> tuple[int, int]:
    size = collection.chunk_size or 0
    overlap = collection.chunk_overlap or 0
    if size <= 0:
        size = settings.RAG_CHUNK_SIZE
    if overlap < 0 or overlap >= size:
        overlap = settings.RAG_CHUNK_OVERLAP
    return size, overlap


@shared_task
def sync_sources():
    """Sync all active knowledge sources"""
    from apps.knowledge.models import Source

    sources = Source.objects.filter(is_active=True, status__in=[Source.Status.PENDING, Source.Status.INDEXED])

    for source in sources:
        # Check if sync is due
        if source.last_synced_at:
            from apps.knowledge.models import Source

            # This would check sync_schedule
            pass

        # Trigger sync
        from apps.events.outbox import enqueue_outbox_event

        enqueue_outbox_event(
            topic='knowledge.source.sync',
            event_key=str(source.id),
            payload={
                'source_id': str(source.id),
                'collection_id': str(source.collection_id),
                'organization_id': str(source.collection.organization_id),
            },
            headers={'trace_id': f'sync-{source.id}'},
        )


@shared_task
def process_document(document_id: str):
    """Index a document: extract → chunk → embed → store vectors in Supabase pgvector."""
    from apps.events.outbox import enqueue_outbox_event
    from apps.knowledge.chunking import chunk_text
    from apps.knowledge.embeddings import embed_texts, embedding_model_name
    from apps.knowledge.extraction import ExtractionError, extract_source_text
    from apps.knowledge.models import Chunk, Document
    from apps.knowledge.vectorstore import (
        upsert_chunk_embeddings,
        vector_store_enabled,
    )

    try:
        document = Document.objects.select_related('source', 'collection').get(id=document_id)
    except Document.DoesNotExist:
        return

    source = document.source
    collection = document.collection
    trace_id = f'doc-{document.id}'

    def fail(document, status, error):
        document.status = Document.Status.FAILED
        document.last_error = str(error)[:2000]
        document.save(update_fields=['status', 'last_error', 'updated_at'])
        enqueue_outbox_event(
            topic='knowledge.document.index_failed',
            event_key=str(document.id),
            payload={'document_id': str(document.id), 'error': str(error)[:2000]},
            headers={'trace_id': trace_id},
        )

    if document.status == Document.Status.DELETED:
        return

    document.status = Document.Status.PARSING
    document.save(update_fields=['status', 'updated_at'])

    try:
        text = extract_source_text(
            source_type=source.source_type,
            config=source.config,
            metadata=document.metadata,
        )
    except ExtractionError as exc:
        fail(document, Document.Status.PARSING, exc)
        return

    content_bytes = len(text.encode('utf-8'))
    document.content_hash = _content_hash(text)
    document.size_bytes = content_bytes
    document.status = Document.Status.CHUNKING
    document.save(update_fields=['content_hash', 'size_bytes', 'status', 'updated_at'])

    chunk_size, chunk_overlap = _collection_chunking(collection)
    try:
        specs = chunk_text(text, size=chunk_size, overlap=chunk_overlap)
    except ValueError as exc:
        fail(document, Document.Status.CHUNKING, exc)
        return

    if not specs:
        document.status = Document.Status.INDEXED
        document.indexed_at = timezone.now()
        document.chunk_count = 0
        document.vector_ids = []
        document.save(update_fields=['status', 'indexed_at', 'chunk_count', 'vector_ids', 'updated_at'])
        _update_collection_counts(collection)
        enqueue_outbox_event(
            topic='knowledge.document.indexed',
            event_key=str(document.id),
            payload={'document_id': str(document.id), 'chunk_count': 0},
            headers={'trace_id': trace_id},
        )
        return

    document.status = Document.Status.EMBEDDING
    document.save(update_fields=['status', 'updated_at'])

    chunk_rows = [
        Chunk(
            id=uuid.uuid4(),
            document=document,
            collection=collection,
            chunk_index=spec.chunk_index,
            content=spec.text,
            token_count=spec.token_count,
            heading_path=spec.heading_path,
            page_number=None,
            offset_start=spec.offset_start,
            offset_end=spec.offset_end,
            vector_id='',
            metadata={'chunk_size': chunk_size, 'chunk_overlap': chunk_overlap},
            acl=document.acl,
        )
        for spec in specs
    ]

    stored_vectors = False
    if vector_store_enabled():
        try:
            provider_model = embedding_model_name()
            embeddings = embed_texts([chunk.content for chunk in chunk_rows])
            Chunk.objects.bulk_create(chunk_rows, batch_size=500)
            vector_ids = []
            for chunk in chunk_rows:
                vector_id = uuid.uuid4().hex
                chunk.vector_id = vector_id
                vector_ids.append(vector_id)
            Chunk.objects.bulk_update(chunk_rows, fields=['vector_id'], batch_size=500)
            upsert_chunk_embeddings(chunk_rows, embeddings, provider_model=provider_model)
            stored_vectors = True
        except Exception as exc:  # provider outage / dimension mismatch etc.
            fail(document, Document.Status.EMBEDDING, exc)
            return
    else:
        # pgvector disabled (e.g. SQLite test DB): persist chunks and metadata
        # only; document is still indexed but not semantically searchable.
        Chunk.objects.bulk_create(chunk_rows, batch_size=500)
        document.vector_ids = [chunk.vector_id for chunk in chunk_rows]

    document.vector_ids = [chunk.vector_id for chunk in chunk_rows if chunk.vector_id]
    document.status = Document.Status.INDEXED
    document.indexed_at = timezone.now()
    document.chunk_count = len(chunk_rows)
    document.last_error = '' if stored_vectors else document.last_error
    document.save(update_fields=['status', 'indexed_at', 'chunk_count', 'vector_ids', 'updated_at'])

    _update_collection_counts(collection)

    enqueue_outbox_event(
        topic='knowledge.document.indexed',
        event_key=str(document.id),
        payload={
            'document_id': str(document.id),
            'chunk_count': len(chunk_rows),
            'vectors_stored': stored_vectors,
            'content_bytes': content_bytes,
        },
        headers={'trace_id': trace_id},
    )


def _update_collection_counts(collection):
    """Refresh aggregate counts on the owning collection."""
    from apps.knowledge.models import Chunk, Document

    collection.document_count = collection.documents.filter(status=Document.Status.INDEXED).count()
    collection.chunk_count = Chunk.objects.filter(collection=collection).count()
    collection.last_indexed_at = timezone.now()
    collection.save(update_fields=['document_count', 'chunk_count', 'last_indexed_at', 'updated_at'])


@shared_task
def rerank_chunks(query: str, chunk_ids: list, top_k: int = 5):
    """Rerank chunks using cross-encoder"""
    # This would use a reranker model
    # For now, return original order
    return chunk_ids[:top_k]
