"""Content extraction for RAG ingestion.

The extraction step is the first stage of the Agentic RAG pipeline: it turns a
knowledge source into normalized UTF-8 text that can be chunked and embedded.
``TEXT`` sources embed content directly; ``URL`` sources are fetched over the
wire (with PDF parsing); other source types require an asset-backed flow that
is wired in later phases.

The extracted text is canonicalized to ``\\n`` line endings and returned as a
single string. Size is bounded to protect memory and embedding budgets.
"""

from __future__ import annotations

import re
from io import BytesIO

from django.conf import settings

from apps.tools.egress import EgressDenied, EgressError, safe_request

MAX_EXTRACTED_BYTES = settings.RAG_MAX_EXTRACTED_BYTES

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_WS_RE = re.compile(r"[ \t]+")


class ExtractionError(RuntimeError):
    """Raised when a knowledge source cannot be converted to text."""


def _clean_html(html: str) -> str:
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    body = body.replace("</p>", "\n\n").replace("<br", "\n<br").replace("</li>", "\n")
    body = re.sub(r"<[^>]+>", " ", body)
    return "\n".join(line.rstrip() for line in _HTML_WS_RE.sub(" ", body).split("\n") if line.strip())


def _cap_size(text: str) -> str:
    encoded = len(text.encode("utf-8"))
    if encoded > MAX_EXTRACTED_BYTES:
        raise ExtractionError(
            f"Source content is {encoded} bytes, exceeding the "
            f"{MAX_EXTRACTED_BYTES}-byte RAG extraction limit."
        )
    return text


def extract_source_text(*, source_type: str, config: dict | None = None, metadata: dict | None = None) -> str:
    """Extract normalized text from a knowledge source.

    ``config`` mirrors ``Source.config``; ``metadata`` mirrors ``Document.metadata``.
    """
    config = config or {}
    metadata = metadata or {}
    source_type = (source_type or "").lower()

    if source_type == "text":
        text = config.get("text") or metadata.get("text") or ""
        if not text.strip():
            raise ExtractionError("TEXT source has no content in its configuration.")
        return _cap_size(text.replace("\r\n", "\n").replace("\r", "\n"))

    if source_type == "url":
        return _fetch_url(str(config.get("url") or metadata.get("url") or ""))

    if source_type == "file":
        text = metadata.get("text") or ""
        if text.strip():
            return _cap_size(text.replace("\r\n", "\n").replace("\r", "\n"))
        asset_id = config.get("asset_id") or metadata.get("asset_id")
        if asset_id:
            return _extract_asset_text(str(asset_id), metadata)
        raise ExtractionError(
            "FILE sources require the asset-backed extraction flow "
            "(uploaded content is not yet linked to ingestion)."
        )

    raise ExtractionError(f"Extraction for source_type={source_type!r} is not implemented yet.")


def _fetch_url(url: str) -> str:
    if not url:
        raise ExtractionError("URL source has no address in its configuration.")
    try:
        response = safe_request(
            "GET",
            url,
            headers={"User-Agent": "JT-Code-RAG/1.0"},
            timeout=settings.RAG_URL_FETCH_TIMEOUT_SECONDS,
            max_bytes=MAX_EXTRACTED_BYTES,
        )
    except (EgressDenied, EgressError) as exc:
        raise ExtractionError(f"URL fetch failed: {exc}") from exc
    if response.status_code >= 400:
        raise ExtractionError(f"URL fetch returned HTTP {response.status_code}.")

    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if "pdf" in content_type:
        return _extract_pdf(response.content)
    if "html" in content_type:
        return _cap_size(_clean_html(response.text))
    if (
        content_type
        and not content_type.startswith("text/")
        and content_type
        not in {
            "application/json",
            "application/xml",
            "application/xhtml+xml",
        }
    ):
        raise ExtractionError(f"Unsupported URL content type: {content_type}.")
    return _cap_size(response.text.replace("\r\n", "\n").replace("\r", "\n"))


def _extract_pdf(content: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # pypdf raises several exception types
        raise ExtractionError(f"PDF extraction failed: {exc}") from exc
    return _cap_size("\n\n".join(pages))


def _extract_asset_text(asset_id: str, metadata: dict) -> str:
    """Read a registered asset through the same egress and size controls as URLs."""
    from apps.assets.models import Asset

    asset = Asset.objects.filter(id=asset_id, status=Asset.Status.READY).first()
    if asset is None:
        raise ExtractionError("The requested knowledge asset is unavailable.")
    expected_org = metadata.get("organization_id")
    if expected_org and str(asset.organization_id) != str(expected_org):
        raise ExtractionError("The requested knowledge asset belongs to another organization.")
    from apps.assets.imagekit import generate_signed_delivery_url

    response = safe_request(
        "GET",
        generate_signed_delivery_url(asset.imagekit_file_path),
        timeout=settings.RAG_URL_FETCH_TIMEOUT_SECONDS,
        max_bytes=MAX_EXTRACTED_BYTES,
    )
    if response.status_code >= 400:
        raise ExtractionError(f"Asset fetch returned HTTP {response.status_code}.")
    content_type = response.headers.get("content-type", "").lower()
    if "pdf" in content_type or asset.format.lower() == "pdf":
        return _extract_pdf(response.content)
    if "html" in content_type:
        return _cap_size(_clean_html(response.text))
    if content_type.startswith("text/") or asset.format.lower() in {"txt", "md", "csv"}:
        return _cap_size(response.text.replace("\r\n", "\n").replace("\r", "\n"))
    raise ExtractionError(f"Unsupported asset content type: {content_type or asset.format}.")
