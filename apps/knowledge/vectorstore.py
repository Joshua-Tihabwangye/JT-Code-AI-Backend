"""Supabase PostgreSQL (pgvector) vector store for Agentic RAG.

Vectors live in the ``Chunk.embedding`` column alongside the content they were
derived from, so metadata filters and authorization scoping reuse the same ORM
querysets as the rest of the application. Tenant isolation is applied by the
caller (verified ``collection_ids``) and *re-checked* inside ``semantic_search``
via ``collection__organization_id`` before any distance is computed.

Only vectors of the current ``embedding_version`` are compared with a query
vector: vectors from a different model or dimensionality live in a different
space and would produce meaningless distances. Searches enable pgvector's
iterative HNSW scan so post-index tenant/ACL filters still return ``top_k``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django.conf import settings
from django.db import connection, transaction


def upsert_chunk_embeddings(
    chunks: Sequence[object],
    embeddings: Sequence[Sequence[float]],
    *,
    provider_model: str,
    embedding_version: str,
) -> int:
    """Persist embedding vectors on already-persisted ``Chunk`` rows."""
    if not chunks:
        return 0
    if len(chunks) != len(embeddings):
        raise ValueError("chunks and embeddings must be aligned.")

    from apps.knowledge.models import Chunk

    chunks_list: list[Any] = list(chunks)
    for chunk, embedding in zip(chunks_list, embeddings, strict=True):
        chunk.embedding = list(embedding)
        chunk.embedding_model = provider_model
        chunk.embedding_dimensions = len(embedding)
        chunk.embedding_version = embedding_version
    Chunk.objects.bulk_update(
        chunks_list,
        fields=["embedding", "embedding_model", "embedding_dimensions", "embedding_version"],
        batch_size=500,
    )
    return len(chunks_list)


def delete_document_embeddings(document_id: Any) -> int:
    """Null out embeddings for every chunk of a document (keeps chunk rows)."""
    from apps.knowledge.models import Chunk

    return Chunk.objects.filter(document_id=document_id, embedding__isnull=False).update(
        embedding=None, embedding_version=""
    )


def semantic_search(
    query_vector: Sequence[float],
    *,
    collection_ids: Sequence[object],
    organization_id: object | None = None,
    user: Any = None,
    top_k: int = 10,
    min_similarity: float | None = None,
    embedding_version: str | None = None,
) -> list[dict[str, Any]]:
    """Cosine retrieval scoped to ``collection_ids`` and the current embedding version.

    Results are ordered by descending cosine similarity. Candidates below
    ``min_similarity`` (default ``VECTOR_MIN_SIMILARITY``) are dropped.
    """
    from pgvector.django import CosineDistance

    from apps.knowledge.access import accessible_chunks
    from apps.knowledge.embeddings import distance_to_similarity
    from apps.knowledge.embeddings import embedding_version as current_embedding_version
    from apps.knowledge.models import Chunk, Document

    version = embedding_version or current_embedding_version()
    queryset = accessible_chunks(
        Chunk.objects.filter(
            embedding__isnull=False,
            embedding_version=version,
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
    threshold = settings.VECTOR_MIN_SIMILARITY if min_similarity is None else min_similarity
    if threshold > 0:
        queryset = queryset.filter(distance__lte=1.0 - threshold)
    queryset = queryset.order_by("distance")[:top_k]

    # Tenant/ACL filters are applied after the HNSW scan; pgvector >= 0.8
    # iterative scans keep reading the index until top_k rows pass them.
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            cursor.execute("SELECT set_config('hnsw.ef_search', %s, true)", [str(max(40, top_k * 4))])
        rows = list(queryset)

    results: list[dict[str, Any]] = []
    for chunk in rows:
        results.append(
            {
                "chunk_id": str(chunk.id),
                "document_id": str(chunk.document_id),
                "document_title": chunk.document.title,
                "source_id": str(chunk.document.source_id),
                "collection_id": str(chunk.collection_id),
                "chunk_index": chunk.chunk_index,
                "content": chunk.content,
                "heading_path": chunk.heading_path,
                "page_number": chunk.page_number,
                "offset_range": [chunk.offset_start, chunk.offset_end],
                "score": round(distance_to_similarity(chunk.distance), 6),
                "embedding_model": chunk.embedding_model,
                "embedding_version": chunk.embedding_version,
                "retrieval_methods": ["semantic"],
            }
        )
    return results
