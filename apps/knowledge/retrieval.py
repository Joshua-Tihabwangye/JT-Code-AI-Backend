"""Hybrid retrieval, context construction, citations, and RAG evaluation.

The vector store deliberately owns only vector similarity.  This module adds
the policy-neutral retrieval stages on top: lexical candidates, reciprocal
rank fusion, a deterministic reranker, a bounded context, and durable
evidence/evaluation records.  Callers must still resolve tenant-authorized
collection ids before entering this module; every database query re-checks the
organization as defence in depth.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction

_TERM_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,}", re.IGNORECASE)
_CITATION_RE = re.compile(r"\[(\d+)]")
_RRF_K = 60


def query_terms(text: str) -> list[str]:
    """Normalized unique lexical terms, preserving query order."""
    seen: set[str] = set()
    terms: list[str] = []
    for term in _TERM_RE.findall(text.lower()):
        if term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def _as_result(chunk, *, score: float, method: str) -> dict:
    return {
        "chunk_id": str(chunk.id),
        "document_id": str(chunk.document_id),
        "document_title": chunk.document.title,
        "collection_id": str(chunk.collection_id),
        "chunk_index": chunk.chunk_index,
        "content": chunk.content,
        "heading_path": chunk.heading_path,
        "page_number": chunk.page_number,
        "offset_range": [chunk.offset_start, chunk.offset_end],
        "score": round(score, 6),
        "embedding_model": chunk.embedding_model,
        "embedding_version": chunk.embedding_version,
        "retrieval_methods": [method],
    }


def lexical_search(
    query: str,
    *,
    collection_ids: Sequence[object],
    organization_id: object,
    user,
    top_k: int,
) -> list[dict]:
    """Lexical candidate retrieval with the same tenant guard as vectors.

    PostgreSQL full-text indexes can be introduced later without changing this
    contract.
    """
    terms = query_terms(query)
    if not terms or not collection_ids:
        return []
    from apps.knowledge.access import accessible_chunks
    from apps.knowledge.models import Chunk, Document

    chunks = accessible_chunks(
        Chunk.objects.filter(
            collection_id__in=collection_ids,
            collection__is_active=True,
            document__status=Document.Status.INDEXED,
            document__source__is_active=True,
        ),
        user,
        organization_id=organization_id,
    )
    chunks = chunks.select_related("document", "collection").only(
        "id",
        "document_id",
        "collection_id",
        "chunk_index",
        "content",
        "heading_path",
        "page_number",
        "offset_start",
        "offset_end",
        "embedding_model",
        "embedding_version",
        "document__title",
    )
    scored: list[dict] = []
    for chunk in chunks.iterator(chunk_size=500):
        words = query_terms(chunk.content)
        word_set = set(words)
        matched = sum(term in word_set for term in terms)
        if not matched:
            continue
        # Term coverage dominates; repeated matches resolve ties without
        # rewarding pathological repetition indefinitely.
        occurrences = sum(chunk.content.lower().count(term) for term in terms)
        score = matched / len(terms) + min(occurrences, len(terms)) / (10 * len(terms))
        scored.append(_as_result(chunk, score=score, method="lexical"))
    return sorted(scored, key=lambda item: (-item["score"], item["chunk_id"]))[:top_k]


def _merge_ranked(*rankings: Iterable[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for ranking in rankings:
        for rank, candidate in enumerate(ranking, start=1):
            key = candidate["chunk_id"]
            current = merged.setdefault(key, {**candidate, "retrieval_methods": []})
            current["rrf_score"] = current.get("rrf_score", 0.0) + 1.0 / (_RRF_K + rank)
            method = (candidate.get("retrieval_methods") or ["semantic"])[0]
            if method not in current["retrieval_methods"]:
                current["retrieval_methods"].append(method)
            current[f"{method}_score"] = candidate.get("score", 0.0)
    return sorted(merged.values(), key=lambda item: (-item["rrf_score"], item["chunk_id"]))


def rerank(query: str, candidates: Iterable[dict], *, top_k: int) -> list[dict]:
    """Deterministically rerank fused candidates using query coverage.

    The score and algorithm label are returned for auditability.  A hosted
    cross-encoder can replace this function behind the same interface without
    weakening the authorization boundary.
    """
    terms = query_terms(query)
    ranked: list[dict] = []
    for candidate in candidates:
        content_terms = set(query_terms(candidate.get("content", "")))
        coverage = sum(term in content_terms for term in terms) / len(terms) if terms else 0.0
        semantic = float(candidate.get("semantic_score", candidate.get("score", 0.0)))
        lexical = float(candidate.get("lexical_score", 0.0))
        item = dict(candidate)
        item["rerank_score"] = round(0.55 * coverage + 0.3 * semantic + 0.15 * lexical, 6)
        item["reranker"] = "deterministic-hybrid-v1"
        ranked.append(item)
    return sorted(ranked, key=lambda item: (-item["rerank_score"], item["chunk_id"]))[:top_k]


def hybrid_search(
    query: str,
    query_vector: Sequence[float] | None,
    *,
    collection_ids: Sequence[object],
    organization_id: object,
    user,
    top_k: int,
    min_similarity: float | None = None,
) -> list[dict]:
    """Fuse tenant-scoped semantic and lexical candidates, then rerank them."""
    candidate_count = max(top_k, int(getattr(settings, "RAG_HYBRID_CANDIDATES", 30)))
    semantic: list[dict] = []
    if query_vector:
        from apps.knowledge.vectorstore import VectorStoreUnavailable, semantic_search

        try:
            semantic = semantic_search(
                query_vector,
                collection_ids=collection_ids,
                organization_id=organization_id,
                user=user,
                top_k=candidate_count,
                min_similarity=min_similarity,
            )
        except VectorStoreUnavailable:
            semantic = []
        for item in semantic:
            item.setdefault("retrieval_methods", ["semantic"])
    lexical = lexical_search(
        query,
        collection_ids=collection_ids,
        organization_id=organization_id,
        user=user,
        top_k=candidate_count,
    )
    return rerank(query, _merge_ranked(semantic, lexical), top_k=top_k)


@dataclass(frozen=True)
class Context:
    text: str
    sources: list[dict]
    token_count: int


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def build_context(sources: Iterable[dict], *, max_tokens: int | None = None) -> Context:
    """Deduplicate evidence and retain only complete chunks within the budget."""
    budget = max_tokens or int(getattr(settings, "RAG_MAX_CONTEXT_TOKENS", 6000))
    used = 0
    selected: list[dict] = []
    parts: list[str] = []
    seen: set[str] = set()
    for source in sources:
        key = source.get("chunk_id") or f"{source.get('document_id')}:{source.get('chunk_index')}"
        if key in seen:
            continue
        content = str(source.get("content") or "").strip()
        tokens = _estimate_tokens(content)
        if not content or used + tokens > budget:
            continue
        seen.add(key)
        item = dict(source)
        item["citation_index"] = len(selected) + 1
        selected.append(item)
        heading = " / ".join(item.get("heading_path") or [])
        location = f", {heading}" if heading else ""
        parts.append(
            f"[{item['citation_index']}] {item.get('document_title', 'Document')}"
            f" (chunk {item.get('chunk_index', '?')}{location})\n{content}"
        )
        used += tokens
    return Context(text="\n\n".join(parts), sources=selected, token_count=used)


def persist_citations(*, job, sources: Iterable[dict]) -> list[dict]:
    """Persist citations only after re-checking source chunks belong to the job tenant."""
    from apps.knowledge.access import accessible_chunks
    from apps.knowledge.models import Chunk, Citation, Document

    source_list = list(sources)
    ids = [item.get("chunk_id") for item in source_list if item.get("chunk_id")]
    chunks = {
        str(chunk.id): chunk
        for chunk in accessible_chunks(
            Chunk.objects.filter(
                id__in=ids,
                collection__is_active=True,
                document__status=Document.Status.INDEXED,
                document__source__is_active=True,
            ),
            job.owner,
            organization_id=job.organization_id,
        ).select_related("document")
    }
    with transaction.atomic():
        Citation.objects.filter(job=job).delete()
        rows = []
        persisted: list[dict] = []
        for source in source_list:
            chunk = chunks.get(str(source.get("chunk_id")))
            if chunk is None:
                continue
            index = len(rows) + 1
            source = dict(source)
            source["citation_index"] = index
            rows.append(
                Citation(
                    job=job,
                    chunk=chunk,
                    document=chunk.document,
                    relevance_score=float(source.get("rerank_score", source.get("score", 0))),
                    citation_index=index,
                    snippet=chunk.content[:1000],
                )
            )
            persisted.append(source)
        Citation.objects.bulk_create(rows)
    return persisted


def evaluate_response(
    *,
    organization,
    query: str,
    sources: Iterable[dict],
    answer: str,
    job=None,
    expected_chunk_ids: Iterable[str] | None = None,
    user=None,
) -> dict:
    """Record deterministic retrieval/citation/groundedness checks for a response."""
    from apps.knowledge.models import RAGEvaluation

    principal = user or (job.owner if job is not None else None)
    if principal is None:
        raise ValueError("A user principal is required for RAG evaluation.")
    source_list = list(sources)
    retrieved = [str(item["chunk_id"]) for item in source_list if item.get("chunk_id")]
    requested_expected = [str(value) for value in (expected_chunk_ids or [])]
    from apps.knowledge.access import accessible_chunks
    from apps.knowledge.models import Chunk

    expected = [
        str(value)
        for value in accessible_chunks(
            Chunk.objects.filter(id__in=requested_expected),
            principal,
            organization_id=organization.id,
        ).values_list("id", flat=True)
    ]
    expected_set, retrieved_set = set(expected), set(retrieved)
    recall = len(expected_set & retrieved_set) / len(expected_set) if expected_set else None
    precision = len(expected_set & retrieved_set) / len(retrieved_set) if expected_set else None
    cited_numbers = {int(value) for value in _CITATION_RE.findall(answer or "")}
    valid_citations = bool(cited_numbers) and cited_numbers.issubset(set(range(1, len(source_list) + 1)))
    # A no-evidence answer is grounded only when the model explicitly abstains.
    normalized_answer = (answer or "").lower()
    abstained = any(
        phrase in normalized_answer
        for phrase in ("do not know", "don't know", "cannot answer", "not enough context")
    )
    grounded = bool(source_list and valid_citations) or bool(not source_list and abstained)
    metrics = {
        "retrievalPrecision": precision,
        "retrievalRecall": recall,
        "citationNumbers": sorted(cited_numbers),
        "citationValid": valid_citations,
        "grounded": grounded,
        "sourceCount": len(source_list),
    }
    passed = grounded and (recall is None or recall >= float(settings.RAG_EVAL_MIN_RECALL))
    evaluation, _ = (
        RAGEvaluation.objects.update_or_create(
            job=job,
            defaults={
                "organization": organization,
                "created_by": principal,
                "query": query,
                "expected_chunk_ids": expected,
                "retrieved_chunk_ids": retrieved,
                "metrics": metrics,
                "passed": passed,
            },
        )
        if job
        else (
            RAGEvaluation.objects.create(
                organization=organization,
                created_by=principal,
                query=query,
                expected_chunk_ids=expected,
                retrieved_chunk_ids=retrieved,
                metrics=metrics,
                passed=passed,
            ),
            True,
        )
    )
    return {"id": str(evaluation.id), "passed": evaluation.passed, **metrics}
