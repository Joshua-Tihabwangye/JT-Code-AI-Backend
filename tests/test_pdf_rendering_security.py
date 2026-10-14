"""User-authored HTML must not make WeasyPrint read local files or fetch URLs."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from apps.conversions.converter import _pdf_from_html
from apps.core.pdf import html_to_pdf
from apps.documents.rendering import render_pdf

MARKER = b"JT_CODE_PDF_SSRF_MARKER_5d1e"


@pytest.fixture
def secret_file(tmp_path):
    path = tmp_path / "secret.txt"
    path.write_bytes(MARKER)
    return path


def _leaked(pdf: bytes) -> bool:
    if MARKER in pdf:
        return True
    reader = PdfReader(io.BytesIO(pdf))
    return any(MARKER in data for blobs in reader.attachments.values() for data in blobs)


def _attack_html(path) -> str:
    uri = path.as_uri()
    return f'<link rel="attachment" href="{uri}"><img src="{uri}"><p>visible text</p>'


def test_document_pdf_export_cannot_embed_local_files(secret_file):
    pdf = render_pdf(SimpleNamespace(title="poc", content=_attack_html(secret_file)))

    assert pdf.startswith(b"%PDF")
    assert not _leaked(pdf)
    assert not PdfReader(io.BytesIO(pdf)).attachments


def test_conversion_pdf_cannot_embed_local_files(secret_file):
    pdf = _pdf_from_html(_attack_html(secret_file))

    assert not _leaked(pdf)
    assert not PdfReader(io.BytesIO(pdf)).attachments


def test_pdf_renderer_never_opens_network_urls(monkeypatch):
    import urllib.request

    def refuse(*_args, **_kwargs):
        raise AssertionError("WeasyPrint attempted a network fetch")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)
    pdf = html_to_pdf('<img src="http://169.254.169.254/latest/meta-data/"><p>ok</p>')

    assert pdf.startswith(b"%PDF")


def test_inline_data_images_still_render():
    pixel = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    assert html_to_pdf(f'<img src="{pixel}">').startswith(b"%PDF")
