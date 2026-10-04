"""Embedding provider adapters for Agentic RAG.

JT-Code stores vectors in Supabase PostgreSQL (pgvector) and generates them
through the configured provider: Google Gemini (REST ``batchEmbedContents``) or
OpenAI. ``echo`` is a deterministic offline backend for the test suite and local
development; it must be selected explicitly and is rejected in staging/production
by settings validation. There is no silent fallback to it.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from typing import Protocol

from django.conf import settings

type EmbeddingVector = list[float]
type EmbeddingBatch = list[EmbeddingVector]


class EmbeddingError(RuntimeError):
    """Base error for embedding provider failures."""

    transient = False


class TransientEmbeddingError(EmbeddingError):
    """A retryable failure (timeout, rate limit, provider outage)."""

    transient = True


class EmbeddingNotConfigured(EmbeddingError):
    """Raised when no usable embedding provider credentials are configured."""


class EmbeddingProvider(Protocol):
    provider_name: str
    model_name: str

    def embed_texts(self, texts: Sequence[str], *, task_type: str) -> EmbeddingBatch: ...


def _normalized_hash_token(text: str, dimensions: int) -> EmbeddingVector:
    """Deterministic pseudo-embedding (offline) for development and tests."""
    vector: list[float] = []
    for axis in range(dimensions):
        digest = hashlib.sha256(f"{axis}:{text[:1024]}".encode()).digest()
        value = int.from_bytes(digest[:4], "big") / 2**32
        vector.append((value * 2.0) - 1.0)
    return vector


class EchoEmbeddingProvider:
    """Offline deterministic backend used by tests and explicit local development."""

    provider_name = "echo"
    model_name = "echo-deterministic"

    def __init__(self, *, dimensions: int | None = None) -> None:
        self._dimensions = dimensions or settings.VECTOR_EMBEDDING_DIMENSIONS

    def embed_texts(self, texts: Sequence[str], *, task_type: str = "document") -> EmbeddingBatch:
        return [_normalized_hash_token(text, self._dimensions) for text in texts]


class OpenAIEmbeddingProvider:
    provider_name = "openai"
    model_name: str

    def __init__(self, *, model: str | None = None) -> None:
        api_key = settings.OPENAI_API_KEY
        if not api_key:
            raise EmbeddingNotConfigured(
                "OpenAI embeddings require OPENAI_API_KEY (RAG_EMBEDDING_PROVIDER=openai)."
            )
        from openai import OpenAI

        self.model_name = model or settings.RAG_EMBEDDING_MODEL
        # Retries are applied uniformly by ``embed_texts``.
        self._client = OpenAI(api_key=api_key, timeout=settings.RAG_EMBEDDING_TIMEOUT_SECONDS, max_retries=0)

    def embed_texts(self, texts: Sequence[str], *, task_type: str = "document") -> EmbeddingBatch:
        import openai

        try:
            response = self._client.embeddings.create(
                model=self.model_name,
                input=[text or " " for text in texts],
                dimensions=settings.VECTOR_EMBEDDING_DIMENSIONS,
            )
        except (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError) as exc:
            raise TransientEmbeddingError(f"OpenAI embeddings unavailable: {exc}") from exc
        except openai.InternalServerError as exc:
            raise TransientEmbeddingError(f"OpenAI embeddings failed: {exc}") from exc
        except openai.OpenAIError as exc:
            raise EmbeddingError(f"OpenAI embeddings rejected the request: {exc}") from exc
        ordered = sorted(response.data, key=lambda item: item.index)
        return [list(item.embedding) for item in ordered]


class GeminiEmbeddingProvider:
    """Gemini ``models/{model}:batchEmbedContents`` over the REST API."""

    provider_name = "gemini"
    model_name: str

    def __init__(self, *, model: str | None = None) -> None:
        if not settings.GEMINI_API_KEY:
            raise EmbeddingNotConfigured(
                "Gemini embeddings require GEMINI_API_KEY (RAG_EMBEDDING_PROVIDER=gemini)."
            )
        self.model_name = model or settings.GEMINI_EMBEDDING_MODEL

    def embed_texts(self, texts: Sequence[str], *, task_type: str = "document") -> EmbeddingBatch:
        from apps.ai_gateway.providers.base import ProviderError, post_json

        model = f"models/{self.model_name}"
        payload = {
            "requests": [
                {
                    "model": model,
                    "content": {"parts": [{"text": text or " "}]},
                    "taskType": "RETRIEVAL_QUERY" if task_type == "query" else "RETRIEVAL_DOCUMENT",
                    "outputDimensionality": settings.VECTOR_EMBEDDING_DIMENSIONS,
                }
                for text in texts
            ]
        }
        try:
            body = post_json(
                f"{settings.GEMINI_API_BASE.rstrip('/')}/{model}:batchEmbedContents",
                headers={"x-goog-api-key": settings.GEMINI_API_KEY},
                payload=payload,
                timeout=settings.RAG_EMBEDDING_TIMEOUT_SECONDS,
                provider="gemini",
            )
        except ProviderError as exc:
            error_class = TransientEmbeddingError if exc.retryable else EmbeddingError
            raise error_class(f"Gemini embeddings failed: {exc}") from exc
        embeddings = body.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise TransientEmbeddingError("Gemini returned an incomplete embedding batch.")
        vectors: EmbeddingBatch = []
        for item in embeddings:
            values = item.get("values") if isinstance(item, dict) else None
            if not isinstance(values, list) or not values:
                raise TransientEmbeddingError("Gemini returned an empty embedding.")
            vectors.append([float(value) for value in values])
        return vectors


def get_embedding_provider() -> EmbeddingProvider:
    """Return the provider selected by ``RAG_EMBEDDING_PROVIDER`` (no implicit fallback)."""
    provider = (settings.RAG_EMBEDDING_PROVIDER or "").lower()
    if provider == "echo":
        return EchoEmbeddingProvider()
    if provider == "openai":
        return OpenAIEmbeddingProvider()
    if provider == "gemini":
        return GeminiEmbeddingProvider()
    raise EmbeddingNotConfigured(
        f"Unsupported RAG_EMBEDDING_PROVIDER={provider!r}. Supported values: gemini, openai, echo."
    )


def _embed_batch_with_retries(
    provider: EmbeddingProvider, texts: Sequence[str], task_type: str
) -> EmbeddingBatch:
    attempts = max(0, int(settings.RAG_EMBEDDING_MAX_RETRIES)) + 1
    for attempt in range(attempts):
        try:
            return provider.embed_texts(texts, task_type=task_type)
        except TransientEmbeddingError:
            if attempt == attempts - 1:
                raise
            time.sleep(min(8.0, 0.5 * 2**attempt))
    raise TransientEmbeddingError("Embedding retries exhausted.")  # pragma: no cover - loop always returns


def embed_texts(texts: Sequence[str], *, task_type: str = "document") -> EmbeddingBatch:
    """Embed texts in provider-sized batches and verify the pgvector column width."""
    if not texts:
        return []
    provider = get_embedding_provider()
    batch_size = max(1, int(settings.RAG_EMBEDDING_BATCH_SIZE))
    embeddings: EmbeddingBatch = []
    for start in range(0, len(texts), batch_size):
        batch = list(texts[start : start + batch_size])
        vectors = _embed_batch_with_retries(provider, batch, task_type)
        if len(vectors) != len(batch):
            raise EmbeddingError("Embedding provider returned a different number of vectors than inputs.")
        embeddings.extend(vectors)
    expected = settings.VECTOR_EMBEDDING_DIMENSIONS
    for vector in embeddings:
        if len(vector) != expected:
            raise EmbeddingError(
                f"Embedding dimensionality mismatch: provider returned {len(vector)} "
                f"dimensions but VECTOR_EMBEDDING_DIMENSIONS={expected}. "
                "Update the setting and migrate the pgvector column width."
            )
    return embeddings


def embed_documents(texts: Sequence[str]) -> EmbeddingBatch:
    return embed_texts(texts, task_type="document")


def embed_query(text: str) -> EmbeddingVector:
    vectors = embed_texts([text], task_type="query")
    if not vectors:
        raise EmbeddingError("Embedding provider returned no vector for the query.")
    return vectors[0]


def embedding_model_name() -> str:
    provider = get_embedding_provider()
    return f"{provider.provider_name}/{provider.model_name}"


def embedding_version() -> str:
    """Version an embedding representation by provider, model and dimensions."""
    return f"{embedding_model_name()}@{settings.VECTOR_EMBEDDING_DIMENSIONS}"


def distance_to_similarity(distance: float) -> float:
    """Convert a pgvector cosine distance to a ``[0, 1]`` similarity score."""
    return max(0.0, min(1.0, 1.0 - distance))
