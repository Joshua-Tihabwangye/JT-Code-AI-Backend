"""Supabase PostgreSQL (pgvector) vector store for Agentic RAG.

Vectors live in the ``Chunk.embedding`` column alongside the content they were
derived from, so metadata filters and authorization scoping reuse the same ORM
querysets as the rest of the application. Tenant isolation is applied by the
caller (verified ``collection_ids``) and *re-checked* inside ``semantic_search``
via ``collection__organization_id`` before any distance is computed.

When ``PGVECTOR_ENABLED`` is false every entry point raises
:class:`VectorStoreUnavailable`.
"""

from __future__ import annotations

from collections.abc import Sequence

from django.conf import settings


class VectorStoreUnavailable(RuntimeError):
    """Raised when pgvector is unavailable on the current connection."""


def vector_store_enabled() -> bool:
    """True when the pgvector feature flag is on (Supabase PostgreSQL only)."""
    return bool(settings.PGVECTOR_ENABLED)


def require_vector_store() -> None:
    if not vector_store_enabled():
        raise VectorStoreUnavailable("The pgvector store is disabled; set PGVECTOR_ENABLED=true.")


def upsert_chunk_embeddings(
    chunks: Sequence[object],
    embeddings: Sequence[Sequence[float]],
    *,
    provider_model: str,
    embedding_version: str,
) -> int:
    """Persist embedding vectors on already-persisted ``Chunk`` rows.

    ``provider_model`` is recorded per row so stale vectors from a superseded
    embedding model can be identified and re-indexed.
    """
    require_vector_store()
    if not chunks:
        return 0
    if len(chunks) != len(embeddings):
        raise ValueError("chunks and embeddings must be aligned.")

    from apps.knowledge.models import Chunk

    dimensions = len(embeddings[0]) if embeddings else None
    chunks_list = list(chunks)
    for chunk, embedding in zip(chunks_list, embeddings, strict=True):
        chunk.embedding = list(embedding)
        chunk.embedding_model = provider_model
        chunk.embedding_dimensions = dimensions
        chunk.embedding_version = embedding_version
    Chunk.objects.bulk_update(
        chunks_list,
        fields=["embedding", "embedding_model", "embedding_dimensions", "embedding_version"],
        batch_size=500,
    )
    return len(chunks_list)


def delete_document_embeddings(document_id) -> int:
    """Null out embeddings for every chunk of a document (keeps chunk rows)."""
    require_vector_store()
    from apps.knowledge.models import Chunk

    updated = 0
    for chunk in (
        Chunk.objects.filter(document_id=document_id, embedding__isnull=False)
        .only("id")
        .iterator(chunk_size=500)
    ):
        chunk.embedding = None
        chunk.save(update_fields=["embedding"])
        updated += 1
    return updated


def semantic_search(
    query_vector: Sequence[float],
    *,
    collection_ids: Sequence[object],
    organization_id: object | None = None,
    user=None,
    top_k: int = 10,
    min_similarity: float | None = None,
) -> list[dict]:
    """Cosine-annealed retrieval scoped to ``collection_ids``.

    Returns results ordered by descending cosine similarity. When
    ``min_similarity`` is provided (defaults to ``RAG_SIMILARITY_THRESHOLD``
    when not ``None``) candidates below the threshold are dropped.
    """
    require_vector_store()
    from pgvector.django import CosineDistance

    from apps.knowledge.access import accessible_chunks
    from apps.knowledge.embeddings import distance_to_similarity
    from apps.knowledge.models import Chunk, Document

    queryset = accessible_chunks(
        Chunk.objects.filter(
            embedding__isnull=False,
            collection_id__in=collection_ids,
            collection__is_active=True,
            document__status=Document.Status.INDEXED,
            document__source__is_active=True,
        ),
        user,
        organization_id=organization_id,
    )
    queryset = queryset.select_related("document", "collection__organization").annotate(
        distance=CosineDistance("embedding", query_vector)
    )
    if min_similarity is None:
        min_similarity = settings.RAG_SIMILARITY_THRESHOLD
    if min_similarity is not None and min_similarity > 0:
        queryset = queryset.filter(distance__lte=1.0 - min_similarity)
    queryset = queryset.order_by("distance")[:top_k]

    results: list[dict] = []
    for chunk in queryset:
        results.append(
            {
                "chunk_id": str(chunk.id),
                "document_id": str(chunk.document_id),
                "document_title": chunk.document.title,
                "collection_id": str(chunk.collection_id),
                "chunk_index": chunk.chunk_index,
                "content": chunk.content,
                "heading_path": chunk.heading_path,
                "page_number": chunk.page_number,
                "offset_range": [chunk.offset_start, chunk.offset_end],
                "score": round(distance_to_similarity(chunk.distance), 6),
                "embedding_model": chunk.embedding_model,
                "embedding_version": chunk.embedding_version,
            }
        )
    return results
