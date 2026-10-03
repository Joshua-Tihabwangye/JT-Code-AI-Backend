"""Embedding provider adapters for Agentic RAG.

JT-Code stores vectors in Supabase PostgreSQL (pgvector) but delegates the
generation of embeddings to a configured provider. Two adapters are bundled
(OpenAI and Google Gemini); ``echo`` is a deterministic, offline backend used
by development and the test suite so no third-party call is required.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Protocol

from django.conf import settings

type EmbeddingVector = list[float]
type EmbeddingBatch = list[EmbeddingVector]

_DEFAULT_BATCH_SIZE = 64


class EmbeddingError(RuntimeError):
    """Base error for embedding provider failures."""


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
    """Offline deterministic backend used for development and tests."""

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
        self._client = OpenAI(
            api_key=api_key,
            timeout=settings.RAG_EMBEDDING_TIMEOUT_SECONDS,
            max_retries=settings.RAG_EMBEDDING_MAX_RETRIES,
        )

    def embed_texts(self, texts: Sequence[str], *, task_type: str = "document") -> EmbeddingBatch:
        embeddings: EmbeddingBatch = []
        for start in range(0, len(texts), _DEFAULT_BATCH_SIZE):
            batch = texts[start : start + _DEFAULT_BATCH_SIZE]
            response = self._client.embeddings.create(
                model=self.model_name,
                input=[text or " " for text in batch],
                dimensions=settings.VECTOR_EMBEDDING_DIMENSIONS,
            )
            embeddings.extend(element.embedding for element in response.data)
        return embeddings


class GeminiEmbeddingProvider:
    provider_name = "gemini"
    model_name: str

    def __init__(self, *, model: str | None = None) -> None:
        api_key = settings.GEMINI_API_KEY
        if not api_key:
            raise EmbeddingNotConfigured(
                "Gemini embeddings require GEMINI_API_KEY (RAG_EMBEDDING_PROVIDER=gemini)."
            )
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        self.model_name = model or settings.GEMINI_EMBEDDING_MODEL
        self._genai = genai

    def embed_texts(self, texts: Sequence[str], *, task_type: str = "document") -> EmbeddingBatch:
        embeddings: EmbeddingBatch = []
        for start in range(0, len(texts), _DEFAULT_BATCH_SIZE):
            batch = texts[start : start + _DEFAULT_BATCH_SIZE]
            response = self._genai.embed_content(
                model=self.model_name,
                content=batch,
                task_type="RETRIEVAL_QUERY" if task_type == "query" else "RETRIEVAL_DOCUMENT",
                output_dimensionality=settings.VECTOR_EMBEDDING_DIMENSIONS,
                request_options={"timeout": settings.RAG_EMBEDDING_TIMEOUT_SECONDS},
            )
            embeddings.extend(response["embedding"])
        return embeddings


def get_embedding_provider() -> EmbeddingProvider:
    """Return the embedding provider selected by ``RAG_EMBEDDING_PROVIDER``."""
    provider = (settings.RAG_EMBEDDING_PROVIDER or "openai").lower()
    if provider == "echo" or (provider == "openai" and settings.DEBUG and not settings.OPENAI_API_KEY):
        return EchoEmbeddingProvider()
    if provider == "openai":
        return OpenAIEmbeddingProvider()
    if provider == "gemini":
        return GeminiEmbeddingProvider()
    raise EmbeddingNotConfigured(
        f"Unsupported RAG_EMBEDDING_PROVIDER={provider!r}. Supported values: openai, gemini, echo."
    )


def embed_texts(texts: Sequence[str], *, task_type: str = "document") -> EmbeddingBatch:
    """Embed a batch of texts, batching as required by the provider."""
    if not texts:
        return []
    provider = get_embedding_provider()
    embeddings = provider.embed_texts(texts, task_type=task_type)
    dimensions = len(embeddings[0]) if embeddings else 0
    expected = settings.VECTOR_EMBEDDING_DIMENSIONS
    if dimensions and expected and dimensions != expected:
        raise EmbeddingError(
            f"Embedding dimensionality mismatch: provider returned {dimensions} "
            f"dimensions but VECTOR_EMBEDDING_DIMENSIONS={expected}. "
            "Update the setting and re-run the pgvector migration for the new column width."
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
