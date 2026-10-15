"""Django-side analytics services. Uploaded code is never executed.

Data handling (pandas, ECharts options, Matplotlib) runs in the isolated engine process
(:mod:`apps.analytics.sandbox`). This module fetches and verifies source bytes,
and re-exports the engine's pure validation helpers for request validation.
"""

from __future__ import annotations

import hashlib
from typing import Any

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
    """Read a READY asset from first-party storage (byte-capped) and verify its checksum.

    The URL is built from server configuration and an internal storage key, never
    user input, so this uses the storage client rather than the public-internet
    egress guard (which would refuse a self-hosted, in-network Supabase Storage).
    """
    import httpx

    from apps.assets.models import Asset
    from apps.assets.supabase_storage import SupabaseStorageError, stream_file

    if asset.status != Asset.Status.READY or not asset.storage_key:
        raise AnalysisError("The dataset asset is not ready.")
    limit = settings.ANALYTICS_MAX_DATASET_BYTES
    if asset.bytes > limit:
        raise AnalysisError("The dataset asset exceeds the configured byte limit.")
    content = bytearray()
    try:
        for chunk in stream_file(asset.storage_key):
            content.extend(chunk)
            if len(content) > limit:
                raise AnalysisError("The dataset asset exceeds the configured byte limit.")
    except (httpx.HTTPError, SupabaseStorageError) as exc:
        raise AnalysisError("The dataset asset could not be downloaded.") from exc
    if asset.bytes and len(content) != asset.bytes:
        raise AnalysisError("The downloaded dataset size does not match its asset record.")
    if asset.checksum_sha256 and sha256(bytes(content)) != asset.checksum_sha256:
        raise AnalysisError("The downloaded dataset failed its integrity check.")
    return bytes(content)


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
