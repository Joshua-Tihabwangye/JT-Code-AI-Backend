from __future__ import annotations

import hashlib
import uuid

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    """Queue active sources that are pending or due according to their cron."""
    from apps.knowledge.models import Source
    from apps.knowledge.scheduling import source_is_due

    sources = Source.objects.filter(
        is_active=True, status__in=[Source.Status.PENDING, Source.Status.INDEXED]
    ).only("id", "status", "sync_schedule", "last_synced_at")

    for source in sources:
        if source_is_due(source):
            sync_source.delay(str(source.id))


@shared_task
def sync_source(source_id: str):
    """Materialize one source into a document and queue secure ingestion.

    A source represents exactly one supported TEXT, URL, or registered FILE
    asset. Unsupported source kinds are not exposed by the model/API.
    """
    from apps.events.outbox import enqueue_outbox_event
    from apps.identity.models import User
    from apps.knowledge.access import normalize_acl
    from apps.knowledge.models import Document, DocumentAccessGrant, Source, SyncRun

    with transaction.atomic():
        try:
            source = (
                Source.objects.select_for_update()
                .select_related("collection__organization")
                .get(id=source_id, is_active=True)
            )
        except Source.DoesNotExist:
            return
        if source.status == Source.Status.PROCESSING:
            return
        run = SyncRun.objects.create(source=source)
        source.status = Source.Status.PROCESSING
        source.last_error = ""
        source.save(update_fields=["status", "last_error", "updated_at"])

        try:
            visibility, user_ids = normalize_acl(source.config.get("acl"))
        except ValueError as exc:
            _fail_sync(source, run, str(exc))
            return
        members = list(
            User.objects.filter(
                id__in=user_ids, organizations__id=source.collection.organization_id
            ).values_list("id", flat=True)
        )
        if len(members) != len(user_ids):
            _fail_sync(source, run, "A restricted ACL contains users outside the source organization.")
            return

        external_id = str(source.config.get("external_id") or source.config.get("url") or source.id)
        document = Document.objects.filter(source=source, external_id=external_id).first()
        created = document is None
        if document is None:
            document = Document(source=source, external_id=external_id, content_hash="pending")
        document.collection = source.collection
        document.title = str(source.config.get("title") or source.name)[:500]
        document.mime_type = str(source.config.get("mime_type") or "text/plain")[:100]
        document.status = Document.Status.PENDING
        document.metadata = {"organization_id": str(source.collection.organization_id)}
        document.acl = source.config.get("acl") or {}
        document.visibility = visibility
        document.last_error = ""
        document.save()
        DocumentAccessGrant.objects.filter(document=document).delete()
        DocumentAccessGrant.objects.bulk_create(
            [
                DocumentAccessGrant(document=document, user_id=user_id, granted_by=source.created_by)
                for user_id in members
            ]
        )
        run.documents_processed = 1
        run.documents_added = int(created)
        run.documents_updated = int(not created)
        run.save(update_fields=["documents_processed", "documents_added", "documents_updated"])
    enqueue_outbox_event(
        topic="knowledge.source.sync",
        event_key=str(source.id),
        payload={
            "source_id": str(source.id),
            "document_id": str(document.id),
            "collection_id": str(source.collection_id),
            "organization_id": str(source.collection.organization_id),
        },
        headers={"trace_id": f"sync-{source.id}"},
    )
    process_document.delay(str(document.id), str(run.id))


@shared_task
def process_document(document_id: str, sync_run_id: str | None = None):
    """Index a document: extract → chunk → embed → store vectors in Supabase pgvector."""
    from apps.events.outbox import enqueue_outbox_event
    from apps.knowledge.chunking import chunk_text
    from apps.knowledge.embeddings import embed_documents, embedding_model_name, embedding_version
    from apps.knowledge.extraction import ExtractionError, extract_source_text
    from apps.knowledge.models import Chunk, Document, SyncRun
    from apps.knowledge.vectorstore import (
        upsert_chunk_embeddings,
        vector_store_enabled,
    )

    try:
        document = Document.objects.select_related("source", "collection").get(id=document_id)
    except Document.DoesNotExist:
        return

    source = document.source
    collection = document.collection
    trace_id = f"doc-{document.id}"

    sync_run = SyncRun.objects.filter(id=sync_run_id).first() if sync_run_id else None

    def fail(document, status, error):
        document.status = Document.Status.FAILED
        document.last_error = str(error)[:2000]
        document.save(update_fields=["status", "last_error", "updated_at"])
        source.status = source.Status.FAILED
        source.last_error = document.last_error
        source.save(update_fields=["status", "last_error", "updated_at"])
        if sync_run:
            sync_run.status = SyncRun.Status.FAILED
            sync_run.error_message = document.last_error
            sync_run.completed_at = timezone.now()
            sync_run.save(update_fields=["status", "error_message", "completed_at"])
        enqueue_outbox_event(
            topic="knowledge.document.index_failed",
            event_key=str(document.id),
            payload={"document_id": str(document.id), "error": str(error)[:2000]},
            headers={"trace_id": trace_id},
        )

    if document.status == Document.Status.DELETED:
        return

    document.status = Document.Status.PARSING
    document.save(update_fields=["status", "updated_at"])

    try:
        text = extract_source_text(
            source_type=source.source_type,
            config=source.config,
            metadata={**document.metadata, "organization_id": str(collection.organization_id)},
        )
    except ExtractionError as exc:
        fail(document, Document.Status.PARSING, exc)
        return

    content_bytes = len(text.encode("utf-8"))
    next_hash = _content_hash(text)
    if (
        document.content_hash == next_hash
        and document.status == Document.Status.PARSING
        and document.chunks.exists()
        and not document.chunks.exclude(embedding_version=_current_embedding_version()).exists()
    ):
        document.status = Document.Status.INDEXED
        document.indexed_at = timezone.now()
        document.last_error = ""
        document.save(update_fields=["status", "indexed_at", "last_error", "updated_at"])
        _update_collection_counts(collection)
        _complete_sync(source, sync_run, chunks_created=0, chunks_deleted=0)
        return
    document.content_hash = next_hash
    document.size_bytes = content_bytes
    document.status = Document.Status.CHUNKING
    document.save(update_fields=["content_hash", "size_bytes", "status", "updated_at"])

    chunk_size, chunk_overlap = _collection_chunking(collection)
    try:
        specs = chunk_text(text, size=chunk_size, overlap=chunk_overlap)
    except ValueError as exc:
        fail(document, Document.Status.CHUNKING, exc)
        return

    if not specs:
        previous_chunk_count = Chunk.objects.filter(document=document).count()
        Chunk.objects.filter(document=document).delete()
        document.status = Document.Status.INDEXED
        document.indexed_at = timezone.now()
        document.chunk_count = 0
        document.vector_ids = []
        document.save(update_fields=["status", "indexed_at", "chunk_count", "vector_ids", "updated_at"])
        _update_collection_counts(collection)
        _update_source_counts(source)
        _complete_sync(
            source,
            sync_run,
            chunks_created=0,
            chunks_deleted=previous_chunk_count,
        )
        enqueue_outbox_event(
            topic="knowledge.document.indexed",
            event_key=str(document.id),
            payload={"document_id": str(document.id), "chunk_count": 0},
            headers={"trace_id": trace_id},
        )
        return

    document.status = Document.Status.EMBEDDING
    document.save(update_fields=["status", "updated_at"])

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
            vector_id=uuid.uuid4().hex,
            metadata={
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
                "source_hash": document.content_hash,
            },
            acl=document.acl,
        )
        for spec in specs
    ]

    stored_vectors = False
    provider_model = ""
    representation_version = ""
    embeddings = []
    if vector_store_enabled():
        try:
            provider_model = embedding_model_name()
            embeddings = embed_documents([chunk.content for chunk in chunk_rows])
            representation_version = embedding_version()
        except Exception as exc:  # provider outage / dimension mismatch etc.
            fail(document, Document.Status.EMBEDDING, exc)
            return

    previous_chunk_count = Chunk.objects.filter(document=document).count()
    # Re-indexing replaces the old representation in one database transaction.
    with transaction.atomic():
        Chunk.objects.filter(document=document).delete()
        Chunk.objects.bulk_create(chunk_rows, batch_size=500)
        if embeddings:
            upsert_chunk_embeddings(
                chunk_rows,
                embeddings,
                provider_model=provider_model,
                embedding_version=representation_version,
            )
            stored_vectors = True

    document.vector_ids = [chunk.vector_id for chunk in chunk_rows if chunk.vector_id]
    document.status = Document.Status.INDEXED
    document.indexed_at = timezone.now()
    document.chunk_count = len(chunk_rows)
    document.last_error = ""
    document.save(
        update_fields=["status", "indexed_at", "chunk_count", "vector_ids", "last_error", "updated_at"]
    )

    _update_collection_counts(collection)
    _update_source_counts(source)
    _complete_sync(
        source,
        sync_run,
        chunks_created=len(chunk_rows),
        chunks_deleted=previous_chunk_count,
    )

    enqueue_outbox_event(
        topic="knowledge.document.indexed",
        event_key=str(document.id),
        payload={
            "document_id": str(document.id),
            "chunk_count": len(chunk_rows),
            "vectors_stored": stored_vectors,
            "content_bytes": content_bytes,
        },
        headers={"trace_id": trace_id},
    )


def _update_collection_counts(collection):
    """Refresh aggregate counts on the owning collection."""
    from apps.knowledge.models import Chunk, Document

    collection.document_count = collection.documents.filter(status=Document.Status.INDEXED).count()
    collection.chunk_count = Chunk.objects.filter(collection=collection).count()
    collection.storage_size_bytes = sum(
        collection.documents.filter(status=Document.Status.INDEXED).values_list("size_bytes", flat=True)
    )
    collection.last_indexed_at = timezone.now()
    collection.save(
        update_fields=[
            "document_count",
            "chunk_count",
            "storage_size_bytes",
            "last_indexed_at",
            "updated_at",
        ]
    )


def _update_source_counts(source):
    from apps.knowledge.models import Document, Source

    source.document_count = source.documents.filter(status=Document.Status.INDEXED).count()
    source.chunk_count = sum(source.documents.values_list("chunk_count", flat=True))
    source.status = Source.Status.INDEXED
    source.last_synced_at = timezone.now()
    source.last_error = ""
    source.save(
        update_fields=[
            "document_count",
            "chunk_count",
            "status",
            "last_synced_at",
            "last_error",
            "updated_at",
        ]
    )


def _current_embedding_version() -> str:
    from apps.knowledge.embeddings import embedding_version

    try:
        return embedding_version()
    except Exception:
        return ""


def _complete_sync(source, sync_run, *, chunks_created: int, chunks_deleted: int) -> None:
    if sync_run is None:
        return
    sync_run.status = sync_run.Status.COMPLETED
    sync_run.chunks_created = chunks_created
    sync_run.chunks_deleted = chunks_deleted
    sync_run.completed_at = timezone.now()
    sync_run.save(update_fields=["status", "chunks_created", "chunks_deleted", "completed_at"])


def _fail_sync(source, sync_run, error: str) -> None:
    source.status = source.Status.FAILED
    source.last_error = error[:2000]
    source.save(update_fields=["status", "last_error", "updated_at"])
    sync_run.status = sync_run.Status.FAILED
    sync_run.error_message = error[:2000]
    sync_run.completed_at = timezone.now()
    sync_run.save(update_fields=["status", "error_message", "completed_at"])
