"""Django-side analytics services. Uploaded code is never executed.

Data handling (pandas, Plotly, Matplotlib) runs in the isolated engine process
(:mod:`apps.analytics.sandbox`). This module fetches and verifies source bytes,
and re-exports the engine's pure validation helpers for request validation.
"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings

from apps.analytics import engine
from apps.analytics.engine import AnalysisError, apply_transform, profile_frame, validate_transform_spec

__all__ = [
    "AnalysisError",
    "apply_transform",
    "bytes_for_asset",
    "dataframe_from_bytes",
    "dataset_bytes",
    "profile_frame",
    "sha256",
    "validate_transform_spec",
]


def _limits() -> dict[str, Any]:
    return {
        "max_bytes": settings.ANALYTICS_MAX_DATASET_BYTES,
        "max_rows": settings.ANALYTICS_MAX_DATASET_ROWS,
        "max_columns": settings.ANALYTICS_MAX_DATASET_COLUMNS,
        "max_cells": settings.ANALYTICS_MAX_DATASET_CELLS,
        "allowed_mime_types": list(settings.ANALYTICS_ALLOWED_MIME_TYPES),
    }


def dataframe_from_bytes(content: bytes, *, mime_type: str) -> Any:
    """In-process parse for tests and tooling; workers use the isolated engine."""
    return engine.dataframe_from_bytes(content, mime_type=mime_type, limits=_limits())


def bytes_for_asset(asset: Any) -> bytes:
    """Download a READY asset through signed, host-pinned egress and verify its checksum."""
    from apps.assets.imagekit import generate_signed_delivery_url
    from apps.assets.models import Asset
    from apps.tools.egress import safe_request

    if asset.status != Asset.Status.READY or not asset.imagekit_file_path:
        raise AnalysisError("The dataset asset is not ready.")
    if asset.bytes > settings.ANALYTICS_MAX_DATASET_BYTES:
        raise AnalysisError("The dataset asset exceeds the configured byte limit.")
    host = urlsplit(settings.IMAGEKIT_ENDPOINT_URL).hostname
    if not host:
        raise AnalysisError("ImageKit delivery is not configured.")
    response = safe_request(
        "GET",
        generate_signed_delivery_url(asset.imagekit_file_path),
        allowed_hosts=[host],
        timeout=settings.ANALYTICS_DOWNLOAD_TIMEOUT_SECONDS,
        max_bytes=settings.ANALYTICS_MAX_DATASET_BYTES,
    )
    if response.status_code != 200:
        raise AnalysisError("The dataset asset could not be downloaded.")
    if asset.bytes and len(response.content) != asset.bytes:
        raise AnalysisError("The downloaded dataset size does not match its asset record.")
    if asset.checksum_sha256 and sha256(response.content) != asset.checksum_sha256:
        raise AnalysisError("The downloaded dataset failed its integrity check.")
    return bytes(response.content)


def dataset_bytes(dataset: Any) -> bytes:
    if dataset.inline_data:
        return str(dataset.inline_data).encode("utf-8")
    if dataset.asset_id:
        return bytes_for_asset(dataset.asset)
    raise AnalysisError("The dataset has no source data.")


def result_bytes(run: Any) -> bytes:
    if not run.result_asset_id:
        raise AnalysisError("The analysis result artifact is unavailable.")
    return bytes_for_asset(run.result_asset)


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
