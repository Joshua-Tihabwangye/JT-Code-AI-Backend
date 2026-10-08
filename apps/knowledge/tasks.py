"""Knowledge ingestion: source sync → extract → chunk → embed → pgvector.

Every stage is idempotent. A document is *claimed* (status + timestamp under a
row lock) before work starts, so concurrent re-index requests cannot interleave;
chunks and their vectors are replaced in one transaction, so readers never see a
half-indexed document. Transient embedding failures are retried with backoff;
permanent failures (bad content, misconfiguration) fail the document. A
scheduled sweep recovers sources/documents abandoned by crashed workers.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

_IN_PROGRESS = ("parsing", "chunking", "embedding")


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _collection_chunking(collection) -> tuple[int, int]:
    size = collection.chunk_size or settings.RAG_CHUNK_SIZE
    overlap = collection.chunk_overlap
    if overlap is None or overlap < 0 or overlap >= size:
        overlap = min(settings.RAG_CHUNK_OVERLAP, size // 2)
    return size, overlap


def _stalled_cutoff():
    return timezone.now() - timedelta(minutes=settings.RAG_INGESTION_STALLED_MINUTES)


def _retry_countdown(attempt: int) -> int:
    return int(min(600, 15 * 2 ** max(0, attempt - 1)))


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
    asset. A source already processing is skipped unless its claim is stale.
    """
    from apps.events.outbox import enqueue_outbox_event
    from apps.identity.models import User
    from apps.knowledge.access import normalize_acl
    from apps.knowledge.models import Document, DocumentAccessGrant, Source, SyncRun

    queued: tuple[str, str] | None = None
    with transaction.atomic():
        try:
            source = (
                Source.objects.select_for_update(of=("self",))
                .select_related("collection__organization")
                .get(id=source_id, is_active=True)
            )
        except Source.DoesNotExist:
            return
        if (
            source.status == Source.Status.PROCESSING
            and source.processing_started_at
            and source.processing_started_at > _stalled_cutoff()
        ):
            return
        run = SyncRun.objects.create(source=source)
        source.status = Source.Status.PROCESSING
        source.processing_started_at = timezone.now()
        source.last_error = ""
        source.save(update_fields=["status", "processing_started_at", "last_error", "updated_at"])
        if source.source_type == Source.SourceType.INTEGRATION:
            # Many documents per source: n8n reads the provider and pushes them back.
            from apps.orchestration.knowledge import IntegrationSyncError, request_integration_sync

            try:
                request_integration_sync(source, run)
            except IntegrationSyncError as exc:
                _fail_sync(source, run, str(exc))
            return

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

        external_id = str(source.config.get("url") or source.config.get("asset_id") or source.id)
        document = (
            Document.objects.select_for_update()
            .filter(source=source)
            .exclude(status=Document.Status.DELETED)
            .first()
        )
        created = document is None
        if document is None:
            document = Document(source=source, external_id=external_id, content_hash="pending")
        document.external_id = external_id
        document.collection = source.collection
        document.title = str(source.config.get("title") or source.name)[:500]
        document.status = Document.Status.PENDING
        document.metadata = {"organization_id": str(source.collection.organization_id)}
        document.acl = source.config.get("acl") or {}
        document.visibility = visibility
        document.index_attempts = 0
        document.last_error = ""
        document.save()
        DocumentAccessGrant.objects.filter(document=document).exclude(user_id__in=members).delete()
        existing = set(
            DocumentAccessGrant.objects.filter(document=document).values_list("user_id", flat=True)
        )
        DocumentAccessGrant.objects.bulk_create(
            [
                DocumentAccessGrant(document=document, user_id=user_id, granted_by=source.created_by)
                for user_id in members
                if user_id not in existing
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
        queued = (str(document.id), str(run.id))
    # Dispatch only after the document and sync run are committed.
    if queued is not None:
        process_document.delay(*queued)


def _claim_document(document_id: str):
    """Lock and claim a document for indexing; ``None`` when another worker owns it."""
    from apps.knowledge.models import Document

    with transaction.atomic():
        document = (
            Document.objects.select_for_update(of=("self",))
            .select_related("source", "collection")
            .filter(id=document_id)
            .first()
        )
        if document is None or document.status == Document.Status.DELETED:
            return None
        if (
            document.status in _IN_PROGRESS
            and document.processing_started_at
            and document.processing_started_at > _stalled_cutoff()
        ):
            return None
        document.status = Document.Status.PARSING
        document.processing_started_at = timezone.now()
        document.index_attempts += 1
        document.last_error = ""
        document.save(
            update_fields=["status", "processing_started_at", "index_attempts", "last_error", "updated_at"]
        )
        return document


def _still_claimed(document) -> bool:
    from apps.knowledge.models import Document

    return Document.objects.filter(
        id=document.id,
        processing_started_at=document.processing_started_at,
        status__in=_IN_PROGRESS,
    ).exists()


@shared_task
def process_document(document_id: str, sync_run_id: str | None = None):
    """Index a document: extract → chunk → embed → store vectors in Supabase pgvector."""
    from apps.events.outbox import enqueue_outbox_event
    from apps.knowledge.chunking import chunk_text
    from apps.knowledge.embeddings import (
        EmbeddingError,
        TransientEmbeddingError,
        embed_documents,
        embedding_model_name,
        embedding_version,
    )
    from apps.knowledge.extraction import ExtractionError, extract_source_text
    from apps.knowledge.models import Chunk, Document, SyncRun

    document = _claim_document(document_id)
    if document is None:
        return
    source = document.source
    collection = document.collection
    trace_id = f"doc-{document.id}"
    sync_run = SyncRun.objects.filter(id=sync_run_id).first() if sync_run_id else None

    def set_status(status: str) -> None:
        Document.objects.filter(id=document.id).update(status=status, updated_at=timezone.now())

    def fail(error: object) -> None:
        message = str(error)[:2000]
        with transaction.atomic():
            Document.objects.filter(id=document.id).exclude(status=Document.Status.DELETED).update(
                status=Document.Status.FAILED,
                last_error=message,
                processing_started_at=None,
                updated_at=timezone.now(),
            )
            if source.source_type != source.SourceType.INTEGRATION:
                # One bad document of an integration does not fail the whole source.
                source.status = source.Status.FAILED
                source.last_error = message
                source.processing_started_at = None
                source.save(update_fields=["status", "last_error", "processing_started_at", "updated_at"])
            if sync_run:
                sync_run.status = SyncRun.Status.FAILED
                sync_run.error_message = message
                sync_run.completed_at = timezone.now()
                sync_run.save(update_fields=["status", "error_message", "completed_at"])
            enqueue_outbox_event(
                topic="knowledge.document.index_failed",
                event_key=str(document.id),
                payload={"document_id": str(document.id), "error": message},
                headers={"trace_id": trace_id},
            )

    try:
        extracted = extract_source_text(
            source_type=source.source_type,
            config=source.config,
            metadata={**document.metadata, "organization_id": str(collection.organization_id)},
        )
    except ExtractionError as exc:
        fail(exc)
        return

    text = extracted.text
    next_hash = _content_hash(text)
    chunk_size, chunk_overlap = _collection_chunking(collection)
    try:
        version = embedding_version()
        provider_model = embedding_model_name()
    except EmbeddingError as exc:
        fail(exc)
        return
    stale_chunks = document.chunks.exclude(embedding_version=version) | document.chunks.exclude(
        metadata__chunk_size=chunk_size, metadata__chunk_overlap=chunk_overlap
    )
    unchanged = document.content_hash == next_hash and document.chunks.exists() and not stale_chunks.exists()
    if unchanged:
        _finish(document, source, collection, sync_run, chunks_created=0, chunks_deleted=0)
        return

    set_status(Document.Status.CHUNKING)
    specs = chunk_text(text, size=chunk_size, overlap=chunk_overlap, page_offsets=extracted.page_offsets)

    set_status(Document.Status.EMBEDDING)
    try:
        embeddings = embed_documents([spec.text for spec in specs]) if specs else []
    except TransientEmbeddingError as exc:
        if document.index_attempts < settings.RAG_INGESTION_MAX_RETRIES:
            Document.objects.filter(id=document.id).update(
                status=Document.Status.PENDING,
                last_error=f"Retrying after transient embedding failure: {exc}"[:2000],
                processing_started_at=None,
                updated_at=timezone.now(),
            )
            process_document.apply_async(
                (str(document.id), sync_run_id), countdown=_retry_countdown(document.index_attempts)
            )
            return
        fail(exc)
        return
    except EmbeddingError as exc:
        fail(exc)
        return

    rows = [
        Chunk(
            document=document,
            collection=collection,
            chunk_index=spec.chunk_index,
            content=spec.text,
            token_count=spec.token_count,
            heading_path=spec.heading_path,
            page_number=spec.page_number,
            offset_start=spec.offset_start,
            offset_end=spec.offset_end,
            metadata={
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
                "source_hash": next_hash,
            },
            acl=document.acl,
            embedding=vector,
            embedding_model=provider_model,
            embedding_dimensions=len(vector),
            embedding_version=version,
        )
        for spec, vector in zip(specs, embeddings, strict=True)
    ]
    with transaction.atomic():
        locked = Document.objects.select_for_update().filter(id=document.id).first()
        if locked is None or not _still_claimed(document):
            # Deleted or re-claimed by a newer run while embedding; discard this result.
            return
        previous = Chunk.objects.filter(document=document).count()
        Chunk.objects.filter(document=document).delete()
        Chunk.objects.bulk_create(rows, batch_size=500)
        locked.content_hash = next_hash
        locked.size_bytes = len(text.encode("utf-8"))
        locked.mime_type = extracted.mime_type[:100]
        locked.page_count = extracted.page_count
        locked.chunk_count = len(rows)
        locked.save(update_fields=["content_hash", "size_bytes", "mime_type", "page_count", "chunk_count"])
    _finish(locked, source, collection, sync_run, chunks_created=len(rows), chunks_deleted=previous)
    enqueue_outbox_event(
        topic="knowledge.document.indexed",
        event_key=str(document.id),
        payload={
            "document_id": str(document.id),
            "chunk_count": len(rows),
            "embedding_version": version,
            "content_bytes": locked.size_bytes,
        },
        headers={"trace_id": trace_id},
    )


def _finish(document, source, collection, sync_run, *, chunks_created: int, chunks_deleted: int) -> None:
    from apps.knowledge.models import Document

    Document.objects.filter(id=document.id).exclude(status=Document.Status.DELETED).update(
        status=Document.Status.INDEXED,
        indexed_at=timezone.now(),
        last_error="",
        processing_started_at=None,
        updated_at=timezone.now(),
    )
    _update_collection_counts(collection)
    _update_source_counts(source)
    _complete_sync(sync_run, chunks_created=chunks_created, chunks_deleted=chunks_deleted)


def _update_collection_counts(collection):
    """Refresh aggregate counts on the owning collection."""
    from django.db.models import Count, Sum

    from apps.knowledge.models import Chunk, Document

    indexed = collection.documents.filter(status=Document.Status.INDEXED).aggregate(
        documents=Count("id"), size=Sum("size_bytes")
    )
    collection.document_count = indexed["documents"] or 0
    collection.storage_size_bytes = indexed["size"] or 0
    collection.chunk_count = Chunk.objects.filter(
        collection=collection, document__status=Document.Status.INDEXED
    ).count()
    collection.last_indexed_at = timezone.now()
    collection.save(
        update_fields=["document_count", "chunk_count", "storage_size_bytes", "last_indexed_at", "updated_at"]
    )


def _update_source_counts(source):
    from django.db.models import Count, Sum

    from apps.knowledge.models import Document, Source

    indexed = source.documents.filter(status=Document.Status.INDEXED).aggregate(
        documents=Count("id"), chunks=Sum("chunk_count")
    )
    source.document_count = indexed["documents"] or 0
    source.chunk_count = indexed["chunks"] or 0
    source.status = Source.Status.INDEXED
    source.last_synced_at = timezone.now()
    source.last_error = ""
    source.processing_started_at = None
    source.save(
        update_fields=[
            "document_count",
            "chunk_count",
            "status",
            "last_synced_at",
            "last_error",
            "processing_started_at",
            "updated_at",
        ]
    )


def _complete_sync(sync_run, *, chunks_created: int, chunks_deleted: int) -> None:
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
    source.processing_started_at = None
    source.save(update_fields=["status", "last_error", "processing_started_at", "updated_at"])
    sync_run.status = sync_run.Status.FAILED
    sync_run.error_message = error[:2000]
    sync_run.completed_at = timezone.now()
    sync_run.save(update_fields=["status", "error_message", "completed_at"])


@shared_task
def recover_stalled_ingestion() -> dict[str, int]:
    """Requeue (or fail after the retry budget) work abandoned by crashed workers."""
    from apps.knowledge.models import Document, Source

    cutoff = _stalled_cutoff()
    requeued = failed = 0
    stalled_documents = Document.objects.filter(
        status__in=_IN_PROGRESS, processing_started_at__lt=cutoff
    ).values_list("id", "index_attempts")
    for document_id, attempts in stalled_documents:
        if attempts >= settings.RAG_INGESTION_MAX_RETRIES:
            Document.objects.filter(id=document_id, status__in=_IN_PROGRESS).update(
                status=Document.Status.FAILED,
                last_error="Indexing stopped before completion and exhausted its retries.",
                processing_started_at=None,
                updated_at=timezone.now(),
            )
            failed += 1
        else:
            process_document.delay(str(document_id))
            requeued += 1
    stalled_sources = Source.objects.filter(
        status=Source.Status.PROCESSING, processing_started_at__lt=cutoff, is_active=True
    ).exclude(documents__status__in=_IN_PROGRESS)
    for source_id in stalled_sources.values_list("id", flat=True):
        sync_source.delay(str(source_id))
        requeued += 1
    return {"requeued": requeued, "failed": failed}


def soft_delete_document(document, *, actor=None) -> None:
    """Hide a document from retrieval immediately and drop its chunks/vectors."""
    from apps.events.outbox import enqueue_outbox_event
    from apps.knowledge.models import Chunk, Document

    with transaction.atomic():
        Document.objects.filter(id=document.id).update(
            status=Document.Status.DELETED,
            deleted_at=timezone.now(),
            chunk_count=0,
            processing_started_at=None,
            updated_at=timezone.now(),
        )
        Chunk.objects.filter(document_id=document.id).delete()
        enqueue_outbox_event(
            topic="knowledge.document.deleted",
            event_key=str(document.id),
            payload={
                "document_id": str(document.id),
                "collection_id": str(document.collection_id),
                "actor_id": str(getattr(actor, "id", "") or ""),
            },
        )
    _update_collection_counts(document.collection)
