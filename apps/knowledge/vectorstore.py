"""Supabase PostgreSQL (pgvector) vector store for Agentic RAG.

Vectors live in the ``Chunk.embedding`` column alongside the content they were
derived from, so metadata filters and authorization scoping reuse the same ORM
querysets as the rest of the application. Tenant isolation is applied by the
caller (verified ``collection_ids``) and *re-checked* inside ``semantic_search``
via ``collection__organization_id`` before any distance is computed.

The store is Postgres-only. On SQLite (the test database) every entry point
raises :class:`VectorStoreUnavailable` so tests exercise mocks instead of
database-specific SQL.
"""

from __future__ import annotations

from collections.abc import Sequence

from django.conf import settings
from django.db import connection


class VectorStoreUnavailable(RuntimeError):
    """Raised when pgvector is unavailable on the current connection."""


def vector_store_enabled() -> bool:
    """True only when pgvector can be queried (Postgres + feature flag)."""
    if not settings.PGVECTOR_ENABLED:
        return False
    return connection.vendor == "postgresql"


def require_vector_store() -> None:
    if not vector_store_enabled():
        raise VectorStoreUnavailable(
            "The pgvector store is only available on a PostgreSQL connection with PGVECTOR_ENABLED=true."
        )


def upsert_chunk_embeddings(
    chunks: Sequence[object],
    embeddings: Sequence[Sequence[float]],
    *,
    provider_model: str,
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
    Chunk.objects.bulk_update(
        chunks_list,
        fields=["embedding", "embedding_model", "embedding_dimensions"],
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

    from apps.knowledge.embeddings import distance_to_similarity
    from apps.knowledge.models import Chunk

    queryset = (
        Chunk.objects.filter(embedding__isnull=False, collection_id__in=collection_ids)
        .select_related("document", "collection__organization")
        .annotate(distance=CosineDistance("embedding", query_vector))
    )
    if organization_id is not None:
        queryset = queryset.filter(collection__organization_id=organization_id)
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
            }
        )
    return results
