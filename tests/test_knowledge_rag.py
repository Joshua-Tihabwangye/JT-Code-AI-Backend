"""Phase 10 unit proofs: chunking, embedding providers and extraction."""

from __future__ import annotations

import io

import pytest

from apps.knowledge import embeddings
from apps.knowledge.chunking import TextChunker, chunk_text
from apps.knowledge.embeddings import (
    EchoEmbeddingProvider,
    EmbeddingError,
    EmbeddingNotConfigured,
    GeminiEmbeddingProvider,
    TransientEmbeddingError,
    embed_texts,
    get_embedding_provider,
)
from apps.knowledge.extraction import ExtractionError, _clean_html, _from_bytes, extract_source_text


def test_consecutive_chunks_overlap_on_word_boundaries():
    text = "\n".join(f"Sentence number {i} explains part of the procedure." for i in range(200))
    chunks = chunk_text(text, size=500, overlap=120)
    assert len(chunks) > 5
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.offset_start < previous.offset_end, "chunks must overlap"
        assert previous.offset_end - current.offset_start <= 120
        assert current.offset_start == 0 or text[current.offset_start - 1].isspace()
    for chunk in chunks:
        assert text[chunk.offset_start : chunk.offset_end] == chunk.text
    assert chunks[-1].offset_end == len(text)


def test_heading_paths_follow_markdown_hierarchy_across_multiline_sections():
    body = "\n".join(["This is a sentence of body text in the section."] * 40)
    text = f"# Guide\n\n## Install\n\n{body}\n\n## Usage\n\n### Linux\n\n{body}"
    paths = [tuple(chunk.heading_path) for chunk in chunk_text(text, size=1000, overlap=200)]
    assert ("Guide", "Install") in paths
    assert ("Guide", "Usage", "Linux") in paths
    assert all(path and path[0] == "Guide" for path in paths)


def test_chunker_records_page_numbers_and_terminates_on_unbroken_text():
    pages = chunk_text("alpha " * 200 + "omega " * 200, size=300, overlap=50, page_offsets=[0, 1200])
    assert pages[0].page_number == 1 and pages[-1].page_number == 2
    assert len(chunk_text("x" * 5000, size=200, overlap=50)) < 60
    with pytest.raises(ValueError):
        TextChunker(size=100, overlap=100)


def test_chunking_is_deterministic():
    text = "# Title\n\n" + "Deterministic chunking matters. " * 300
    assert chunk_text(text, size=400, overlap=80) == chunk_text(text, size=400, overlap=80)


def test_echo_provider_is_deterministic_and_must_be_selected_explicitly(settings):
    provider = EchoEmbeddingProvider()
    assert provider.embed_texts(["a"]) == provider.embed_texts(["a"])
    assert len(provider.embed_texts(["a"])[0]) == settings.VECTOR_EMBEDDING_DIMENSIONS
    settings.RAG_EMBEDDING_PROVIDER = "openai"
    settings.OPENAI_API_KEY = ""
    settings.DEBUG = True
    with pytest.raises(EmbeddingNotConfigured):
        get_embedding_provider()  # no silent fallback to fake vectors


def test_embed_texts_batches_and_rejects_dimension_mismatch(settings, monkeypatch):
    settings.RAG_EMBEDDING_BATCH_SIZE = 2
    calls = []

    class Recorder:
        provider_name, model_name = "fake", "fake-model"

        def embed_texts(self, texts, *, task_type):
            calls.append(list(texts))
            return [[0.1] * settings.VECTOR_EMBEDDING_DIMENSIONS for _ in texts]

    monkeypatch.setattr(embeddings, "get_embedding_provider", lambda: Recorder())
    assert len(embed_texts(["a", "b", "c"])) == 3
    assert calls == [["a", "b"], ["c"]]
    assert embed_texts([]) == []

    class Wrong(Recorder):
        def embed_texts(self, texts, *, task_type):
            return [[0.1] * 8 for _ in texts]

    monkeypatch.setattr(embeddings, "get_embedding_provider", lambda: Wrong())
    with pytest.raises(EmbeddingError, match="dimensionality mismatch"):
        embed_texts(["a"])


def test_transient_embedding_errors_are_retried(settings, monkeypatch):
    settings.RAG_EMBEDDING_MAX_RETRIES = 1
    monkeypatch.setattr(embeddings.time, "sleep", lambda seconds: None)
    attempts = []

    class Flaky:
        provider_name, model_name = "fake", "fake-model"

        def embed_texts(self, texts, *, task_type):
            attempts.append(1)
            if len(attempts) == 1:
                raise TransientEmbeddingError("rate limited")
            return [[0.2] * settings.VECTOR_EMBEDDING_DIMENSIONS for _ in texts]

    monkeypatch.setattr(embeddings, "get_embedding_provider", lambda: Flaky())
    assert len(embed_texts(["a"])) == 1
    assert len(attempts) == 2


def test_gemini_provider_uses_batch_embed_rest_contract(settings, monkeypatch):
    settings.GEMINI_API_KEY = "test-gemini-key"  # pragma: allowlist secret
    settings.GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"
    captured = {}

    def fake_post_json(url, *, headers, payload, timeout, provider):
        captured.update(url=url, headers=headers, payload=payload)
        return {
            "embeddings": [
                {"values": [0.5] * settings.VECTOR_EMBEDDING_DIMENSIONS} for _ in payload["requests"]
            ]
        }

    monkeypatch.setattr("apps.ai_gateway.providers.base.post_json", fake_post_json)
    vectors = GeminiEmbeddingProvider().embed_texts(["hello", "world"], task_type="query")
    assert len(vectors) == 2
    assert captured["url"].endswith("/models/gemini-embedding-001:batchEmbedContents")
    assert captured["headers"] == {"x-goog-api-key": "test-gemini-key"}
    request = captured["payload"]["requests"][0]
    assert request["taskType"] == "RETRIEVAL_QUERY"
    assert request["outputDimensionality"] == settings.VECTOR_EMBEDDING_DIMENSIONS


def test_gemini_provider_maps_retryable_failures_to_transient(settings, monkeypatch):
    from apps.ai_gateway.providers.base import ProviderBadRequest, ProviderRateLimited

    settings.GEMINI_API_KEY = "test-gemini-key"  # pragma: allowlist secret

    def rate_limited(*args, **kwargs):
        raise ProviderRateLimited("slow down")

    monkeypatch.setattr("apps.ai_gateway.providers.base.post_json", rate_limited)
    with pytest.raises(TransientEmbeddingError):
        GeminiEmbeddingProvider().embed_texts(["x"])

    def bad_request(*args, **kwargs):
        raise ProviderBadRequest("bad")

    monkeypatch.setattr("apps.ai_gateway.providers.base.post_json", bad_request)
    with pytest.raises(EmbeddingError) as excinfo:
        GeminiEmbeddingProvider().embed_texts(["x"])
    assert not excinfo.value.transient


def test_html_extraction_keeps_structure_and_drops_scripts():
    html = (
        "<html><head><title>x</title><script>steal()</script></head><body>"
        "<h2>Refunds</h2><p>Within &amp; 14 days.</p><style>p{}</style><ul><li>One</li><li>Two</li></ul>"
        "</body></html>"
    )
    text = _clean_html(html)
    assert "steal" not in text and "p{}" not in text
    assert "## Refunds" in text and "Within & 14 days." in text
    assert "- One\n- Two" in text


def test_docx_extraction_preserves_headings():
    from docx import Document as DocxDocument

    document = DocxDocument()
    document.add_heading("Policies", level=1)
    document.add_paragraph("Expenses need approval.")
    buffer = io.BytesIO()
    document.save(buffer)
    extracted = _from_bytes(buffer.getvalue(), content_type="", extension="docx")
    assert extracted.text.startswith("# Policies")
    assert "Expenses need approval." in extracted.text


def test_pdf_extraction_records_page_offsets():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(ExtractionError, match="no extractable text"):
        _from_bytes(buffer.getvalue(), content_type="application/pdf")


def test_text_and_unsupported_sources():
    extracted = extract_source_text(source_type="text", config={"text": "Line one\r\nLine two"})
    assert extracted.text == "Line one\nLine two"
    assert extracted.page_count == 0
    with pytest.raises(ExtractionError):
        extract_source_text(source_type="text", config={"text": "   "})
    with pytest.raises(ExtractionError, match="asset_id"):
        extract_source_text(source_type="file", config={})
    with pytest.raises(ExtractionError, match="no content"):
        extract_source_text(source_type="integration", config={})  # nothing pushed by n8n yet
    with pytest.raises(ExtractionError, match="not supported"):
        extract_source_text(source_type="ftp", config={})
    with pytest.raises(ExtractionError, match="Unsupported content type"):
        _from_bytes(b"\x00\x01", content_type="application/octet-stream")
