import pytest
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APIClient

from apps.events.models import OutboxEvent
from apps.identity.models import Organization
from apps.knowledge import embeddings, vectorstore
from apps.knowledge.chunking import TextChunker, chunk_text
from apps.knowledge.embeddings import (
    EchoEmbeddingProvider,
    EmbeddingNotConfigured,
    embed_texts,
    get_embedding_provider,
)
from apps.knowledge.extraction import ExtractionError, extract_source_text
from apps.knowledge.models import Chunk, Collection, Document, Source
from apps.knowledge.tasks import process_document


@pytest.fixture
def organization(db, user):
    org = Organization.objects.create(name="Acme", slug="acme")
    user.organizations.add(org)
    return org


@pytest.fixture
def collection(db, organization, user):
    return Collection.objects.create(
        organization=organization,
        name="Product docs",
        embedding_provider="echo",
        embedding_model="echo-deterministic",
        embedding_dimensions=1536,
        chunk_size=100,
        chunk_overlap=20,
        created_by=user,
    )


@pytest.fixture
def text_source(db, collection, user):
    return Source.objects.create(
        collection=collection,
        source_type=Source.SourceType.TEXT,
        name="Manual",
        config={"text": "JT-Code onboarding. " * 50},
        created_by=user,
    )


def test_chunking_produces_overlapping_coverage():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(8))
    chunks = chunk_text(text, size=150, overlap=30)
    assert len(chunks) >= 3
    first = chunks[0]
    assert first.chunk_index == 0
    assert first.offset_start == 0
    assert first.offset_end <= len(text)
    assert first.token_count > 0
    covered = sum(len(c.text) for c in chunks)
    assert covered >= len(text)
    # Overlap window means consecutive chunks can share content.
    assert text[chunks[1].offset_start : chunks[1].offset_end] == chunks[1].text


def test_chunking_tracks_headings():
    heading = "## Overview\n"
    filler = "word " * 80
    text = f"{heading}{filler}\n## Details\n{filler}"
    chunks = TextChunker(size=80, overlap=10).chunk(text)
    assert len(chunks) >= 2
    paths = [chunk.heading_path for chunk in chunks]
    assert any("Overview" in path for path in paths)
    assert any("Details" in path for path in paths)


def test_echo_provider_deterministic_dimensions():
    provider = EchoEmbeddingProvider()
    batch = provider.embed_texts(["alpha", "beta"])
    assert len(batch) == 2
    assert all(len(vector) == 1536 for vector in batch)
    again = EchoEmbeddingProvider().embed_texts(["alpha", "beta"])
    assert batch == again
    assert batch[0] != batch[1]


def test_embed_texts_empty_and_dimension_check(settings, monkeypatch):
    settings.RAG_EMBEDDING_PROVIDER = "echo"
    assert embed_texts([]) == []
    vectors = embed_texts(["hi"] * 3)
    assert len(vectors) == 3
    assert all(len(v) == 1536 for v in vectors)

    class WrongDimsProvider:
        provider_name = "wrong"
        model_name = "wrong"

        def embed_texts(self, texts):
            return [[0.0] * 64] * len(texts)

    monkeypatch.setattr(embeddings, "get_embedding_provider", lambda: WrongDimsProvider())
    settings.VECTOR_EMBEDDING_DIMENSIONS = 1536
    from apps.knowledge.embeddings import EmbeddingError

    with pytest.raises(EmbeddingError):
        embed_texts(["x"])


def test_unsupported_provider_raises(settings):
    settings.RAG_EMBEDDING_PROVIDER = "bogus"
    with pytest.raises(EmbeddingNotConfigured):
        get_embedding_provider()


def test_openai_provider_requires_key(settings, monkeypatch):
    settings.RAG_EMBEDDING_PROVIDER = "openai"
    settings.DEBUG = False
    settings.OPENAI_API_KEY = ""
    with pytest.raises(EmbeddingNotConfigured):
        get_embedding_provider()


def test_extract_text_source():
    text = extract_source_text(source_type="text", config={"text": "line1\nline2"})
    assert text == "line1\nline2"


def test_extract_empty_text_raises():
    with pytest.raises(ExtractionError):
        extract_source_text(source_type="text", config={})


def test_extract_file_without_content_raises():
    with pytest.raises(ExtractionError):
        extract_source_text(source_type="file", config={})


def test_vector_store_unavailable_when_pgvector_disabled(settings):
    settings.PGVECTOR_ENABLED = False
    assert vectorstore.vector_store_enabled() is False
    with pytest.raises(vectorstore.VectorStoreUnavailable):
        vectorstore.require_vector_store()
    with pytest.raises(vectorstore.VectorStoreUnavailable):
        vectorstore.semantic_search([0.0] * 4, collection_ids=[], top_k=3)


@pytest.mark.django_db
def test_search_requires_query(api_client, user):
    api_client.force_authenticate(user=user)
    response = api_client.post(reverse("knowledge-search"), {}, format="json")
    assert response.status_code == 400


@pytest.mark.django_db
def test_search_excludes_collections_outside_org(api_client, user, collection):
    other = Organization.objects.create(name="Rival", slug="rival")
    rival = Collection.objects.create(organization=other, name="Secret", embedding_provider="echo")
    api_client.force_authenticate(user=user)
    response = api_client.post(
        reverse("knowledge-search"),
        {"query": "hello", "collection_ids": [str(rival.id)]},
        format="json",
    )
    body = response.json()
    assert response.status_code == 200
    assert body["results"] == []
    assert body["message"] == "No accessible collections"


@pytest.mark.django_db
def test_search_returns_503_when_pgvector_unavailable(api_client, user, collection, settings):
    settings.PGVECTOR_ENABLED = False
    api_client.force_authenticate(user=user)
    response = api_client.post(
        reverse("knowledge-search"),
        {"query": "hello", "collection_ids": [str(collection.id)]},
        format="json",
    )
    assert response.status_code == 503


@pytest.mark.django_db
def test_search_with_mocked_store_returns_results(api_client, user, collection, monkeypatch):
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts: [[0.25] * 1536],
    )
    monkeypatch.setattr(
        vectorstore,
        "semantic_search",
        lambda *args, **kwargs: [
            {
                "chunk_id": "abc",
                "document_id": "doc",
                "document_title": "Manual",
                "collection_id": str(collection.id),
                "chunk_index": 0,
                "content": "sample chunk",
                "heading_path": [],
                "page_number": None,
                "offset_range": [0, 10],
                "score": 0.95,
                "embedding_model": "echo",
            }
        ],
    )
    api_client.force_authenticate(user=user)
    response = api_client.post(
        reverse("knowledge-search"),
        {"query": "hello", "collection_ids": [str(collection.id)]},
        format="json",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result_count"] == 1
    assert body["results"][0]["score"] == 0.95


@pytest.mark.django_db
def test_process_document_indexes_text_source(db, text_source):
    document = Document.objects.create(
        source=text_source,
        collection=text_source.collection,
        title="Manual",
        content_hash="pending",
        mime_type="text/plain",
        size_bytes=0,
        status=Document.Status.PENDING,
    )
    process_document(str(document.id))

    document.refresh_from_db()
    assert document.status == Document.Status.INDEXED
    assert document.chunk_count > 0
    assert document.last_error == ""
    assert Chunk.objects.filter(document=document).count() == document.chunk_count
    assert OutboxEvent.objects.filter(topic__endswith="knowledge.document.indexed").exists()
    # Supabase pgvector stores one embedding per chunk.
    assert len(document.vector_ids) == document.chunk_count
    assert not Chunk.objects.filter(document=document, embedding__isnull=True).exists()


@pytest.mark.django_db
def test_process_document_records_failure(db, collection, user):
    source = Source.objects.create(
        collection=collection,
        source_type=Source.SourceType.FILE,
        name="Broken",
        config={},
        created_by=user,
    )
    document = Document.objects.create(
        source=source,
        collection=collection,
        title="Broken",
        content_hash="x",
        status=Document.Status.PENDING,
    )
    process_document(str(document.id))
    document.refresh_from_db()
    assert document.status == Document.Status.FAILED
    assert "extraction" in document.last_error.lower()


def test_reindex_endpoint_returns_accepted(api_client, user):
    url = "/api/v1/knowledge/documents/nonexistent/reindex/"
    api_client.force_authenticate(user=user)
    response = api_client.post(url, {}, format="json")
    assert response.status_code in (404, 405)


def test_health_endpoint():
    client = APIClient()
    response = client.get("/api/v1/health/live/")
    assert response.status_code == 200


def test_cache_cleanup():
    cache.clear()
    assert cache.get("nothing") is None
