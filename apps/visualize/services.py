"""Django-side services for the visualization tool.

All data processing runs in the isolated visualization engine
(:mod:`apps.visualize.viz_engine`) through :func:`apps.analytics.sandbox.run_engine`;
this module only moves verified bytes, stores results and builds exports.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import threading
import uuid
from functools import cache
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.cache import caches
from django.utils import timezone

from apps.analytics.models import Dataset
from apps.analytics.sandbox import run_engine
from apps.analytics.services import AnalysisError, dataset_bytes

VIZ_ENGINE = Path(__file__).with_name("viz_engine.py")
_RESULT_PREFIX = "viz-result"


# Pure functions of (data, request): their answers are reused from the cache for identical requests.
_CACHEABLE_OPERATIONS = frozenset(
    {"viz_render", "viz_table", "viz_insights", "viz_preview", "viz_quality", "viz_predict"}
)
_ENGINE_CACHE_SECONDS = 30 * 60
_ENGINE_CACHE_MAX_BYTES = 2 * 1024 * 1024
_DATA_CACHE_MAX_BYTES = 96 * 1024 * 1024
_data_cache: dict[str, bytes] = {}  # data digest -> bytes, most recent last
_data_cache_lock = threading.Lock()


def _data_digest(dataset: Dataset | None, content: bytes) -> str:
    """Identity of the data an engine call reads, without downloading it when the store knows it."""
    if dataset is not None:
        asset = dataset.asset if dataset.asset_id else None
        if asset is not None and asset.checksum_sha256:
            return f"asset:{asset.checksum_sha256}"
        return "inline:" + hashlib.sha256(str(dataset.inline_data or "").encode("utf-8")).hexdigest()
    return "bytes:" + hashlib.sha256(content).hexdigest()


def _dataset_content(dataset: Dataset, digest: str) -> bytes:
    """Dataset bytes, kept in a small per-process cache so they are not re-downloaded per request."""
    with _data_cache_lock:
        cached = _data_cache.pop(digest, None)
        if cached is not None:
            _data_cache[digest] = cached
            return cached
    content = dataset_bytes(dataset)
    if len(content) <= _DATA_CACHE_MAX_BYTES // 4:
        with _data_cache_lock:
            _data_cache[digest] = content
            while sum(len(item) for item in _data_cache.values()) > _DATA_CACHE_MAX_BYTES:
                _data_cache.pop(next(iter(_data_cache)))
    return content


def engine(
    operation: str, dataset: Dataset | None = None, content: bytes = b"", **request: Any
) -> dict[str, Any]:
    if dataset is not None:
        request.setdefault("mime_type", dataset.mime_type)
    digest = _data_digest(dataset, content)
    key = None
    if operation in _CACHEABLE_OPERATIONS:
        fingerprint = json.dumps(
            {"op": operation, "data": digest, "request": request}, sort_keys=True, default=str
        )
        key = "viz-engine:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        hit = caches["default"].get(key)
        if hit is not None:
            return dict(hit)
    if dataset is not None:
        content = _dataset_content(dataset, digest)
    response = run_engine(operation, content, script=VIZ_ENGINE, **request)
    if key is not None and len(json.dumps(response, default=str)) <= _ENGINE_CACHE_MAX_BYTES:
        caches["default"].set(key, response, _ENGINE_CACHE_SECONDS)
    return response


def ensure_profile(dataset: Dataset) -> dict[str, Any]:
    """The stored column profile, computed in the sandbox the first time it is needed."""
    if dataset.profile.get("columnProfiles"):
        return dict(dataset.profile)
    profile = engine("viz_preview", dataset, rows=1)["profile"]
    _store_profile(dataset, profile)
    return dict(profile)


def _store_profile(dataset: Dataset, profile: dict[str, Any]) -> None:
    dataset.profile = profile
    dataset.schema = {"columns": profile.get("columns", []), "dtypes": profile.get("dtypes", {})}
    dataset.row_count = profile.get("rows", 0)
    dataset.save(update_fields=["profile", "schema", "row_count", "updated_at"])


def create_dataset(
    *,
    name: str,
    csv: bytes,
    profile: dict[str, Any],
    owner: Any,
    organization: Any,
    provenance: dict[str, Any],
) -> Dataset:
    """Store a normalized CSV inline when small, otherwise as a private generated asset."""
    checksum = hashlib.sha256(csv).hexdigest()
    asset = None
    inline = ""
    if len(csv) <= settings.ANALYTICS_MAX_INLINE_BYTES:
        inline = csv.decode("utf-8")
    else:
        if len(csv) > settings.ANALYTICS_MAX_DATASET_BYTES:
            raise AnalysisError("The dataset exceeds the configured byte limit.")
        from apps.assets.services import register_generated_asset

        asset = register_generated_asset(
            csv,
            owner=owner,
            organization=organization,
            file_name=f"{uuid.uuid4().hex}.csv",
            kind="analytics/datasets",
            content_type="text/csv",
            provenance=provenance,
        )
    return Dataset.objects.create(
        organization=organization,
        owner=owner,
        asset=asset,
        name=name[:255],
        mime_type="text/csv",
        inline_data=inline,
        schema={"columns": profile.get("columns", []), "dtypes": profile.get("dtypes", {})},
        profile=profile,
        row_count=profile.get("rows", 0),
        byte_size=len(csv),
        source_checksum_sha256=checksum,
    )


# --- Short-lived prediction results ----------------------------------------------------------


def _result_key(organization_id: Any, user_id: Any, result_id: str) -> str:
    return f"{_RESULT_PREFIX}:{organization_id}:{user_id}:{result_id}"


def remember_result(organization_id: Any, user_id: Any, payload: dict[str, Any]) -> str:
    result_id = uuid.uuid4().hex
    caches["default"].set(
        _result_key(organization_id, user_id, result_id),
        payload,
        timeout=settings.VISUALIZE_RESULT_TTL_SECONDS,
    )
    return result_id


def recall_result(organization_id: Any, user_id: Any, result_id: str) -> dict[str, Any] | None:
    if not result_id.isalnum() or len(result_id) != 32:
        return None
    value = caches["default"].get(_result_key(organization_id, user_id, result_id))
    return dict(value) if isinstance(value, dict) else None


# --- Report exports ----------------------------------------------------------------------------


STATIC = Path(__file__).with_name("static")


@cache
def _static(name: str) -> str:
    """Vendored client files (Apache ECharts, echarts-gl, the hydration script, the world map)."""
    text = (STATIC / name).read_text(encoding="utf-8")
    return text.replace("</script", "<\\/script")


def option_needs(option: dict[str, Any]) -> tuple[bool, bool]:
    """Whether an ECharts option needs echarts-gl (3D) and/or the world map."""
    series = option.get("series") or []
    gl = any(str(item.get("type", "")).endswith("3D") or item.get("type") == "surface" for item in series)
    world = "geo" in option or any(item.get("type") == "map" for item in series)
    return gl, world


def update_dataset_content(dataset: Dataset, *, csv: bytes, profile: dict[str, Any], user: Any) -> Dataset:
    """Replace a dataset's data with a corrected version (the owner's own edits or a re-upload)."""
    if len(csv) > settings.ANALYTICS_MAX_DATASET_BYTES:
        raise AnalysisError("The dataset exceeds the configured byte limit.")
    old_asset = dataset.asset
    if len(csv) <= settings.ANALYTICS_MAX_INLINE_BYTES:
        dataset.inline_data, dataset.asset = csv.decode("utf-8"), None
    else:
        from apps.assets.services import register_generated_asset

        dataset.inline_data = ""
        dataset.asset = register_generated_asset(
            csv,
            owner=user,
            organization=dataset.organization,
            file_name=f"{uuid.uuid4().hex}.csv",
            kind="analytics/datasets",
            content_type="text/csv",
            provenance={"kind": "visualize-edit", "dataset_id": str(dataset.id)},
        )
    dataset.profile = profile
    dataset.schema = {"columns": profile.get("columns", []), "dtypes": profile.get("dtypes", {})}
    dataset.row_count = profile.get("rows", 0)
    dataset.byte_size = len(csv)
    dataset.source_checksum_sha256 = hashlib.sha256(csv).hexdigest()
    dataset.save()
    if old_asset is not None and old_asset.id != getattr(dataset.asset, "id", None):
        from apps.assets.services import soft_delete_asset

        soft_delete_asset(old_asset)
    return dataset


def _script_json(value: Any) -> str:
    return (
        json.dumps(value, separators=(",", ":"), default=str)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


REPORT_CSS = (Path(__file__).with_name("report.css")).read_text(encoding="utf-8")


def _stats_html(stats: list[dict[str, Any]]) -> str:
    if not stats:
        return ""
    items = "".join(
        f"<li>{html.escape(str(s.get('label', '')))}<b>{html.escape(str(s.get('value', '')))}</b></li>"
        for s in stats[:12]
    )
    return f'<ul class="stats">{items}</ul>'


def _explanation_html(explanation: dict[str, Any] | None) -> str:
    if not explanation:
        return ""
    parts = [
        f'<p class="about">{html.escape(explanation.get("about", ""))}</p>'
        if explanation.get("about")
        else "",
        f'<p class="how"><b>How to read it:</b> {html.escape(explanation.get("howToRead", ""))}</p>'
        if explanation.get("howToRead")
        else "",
        f'<p class="insight"><b>Key takeaway:</b> {html.escape(explanation.get("insight", ""))}</p>'
        if explanation.get("insight")
        else "",
    ]
    return f'<div class="explain">{"".join(parts)}</div>'


def report_html(
    *, title: str, description: str, theme: str, blocks: list[dict[str, Any]], author: str = ""
) -> str:
    """A standalone, interactive HTML report (Apache ECharts inlined; works offline)."""
    sections: list[str] = []
    charts: list[dict[str, Any]] = []
    needs_gl = needs_world = False
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            sections.append(f'<section class="card text">{html.escape(block.get("text", ""))}</section>')
            continue
        element = f"chart-{index}"
        option = dict(block.get("option") or {})
        uses_gl, uses_world = option_needs(option)
        needs_gl, needs_world = needs_gl or uses_gl, needs_world or uses_world
        charts.append({"id": element, "option": option})
        note = f'<p class="note">{html.escape(block["note"])}</p>' if block.get("note") else ""
        sections.append(
            f'<section class="card"><h2>{html.escape(block.get("title", ""))}</h2>'
            f'<p class="sub">{html.escape(block.get("subtitle", ""))}</p>'
            f"{_explanation_html(block.get('explanation'))}"
            f'<div class="plot" id="{element}"></div>{_stats_html(block.get("stats") or [])}{note}</section>'
        )
    generated = timezone.now().strftime("%d %b %Y, %H:%M UTC")
    chips = [f"{len(charts)} interactive chart{'s' if len(charts) != 1 else ''}", f"Generated {generated}"]
    if author:
        chips.append(f"By {author}")
    scripts = f"<script>{_static('vendor/echarts.min.js')}</script>"
    if needs_gl:
        scripts += f"<script>{_static('vendor/echarts-gl.min.js')}</script>"
    scripts += f"<script>{_static('hydrate.js')}</script>"
    register_map = f"echarts.registerMap('world', {_static('world.json')});" if needs_world else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{REPORT_CSS}</style>{scripts}</head>"
        f'<body class="{"dark" if theme == "dark" else "light"}">'
        f'<header><div class="wrap"><h1>{html.escape(title)}</h1>'
        f'<p>{html.escape(description)}</p><div class="meta">'
        + "".join(f'<span class="chip">{html.escape(chip)}</span>' for chip in chips)
        + f'</div></div></header><main><div class="wrap">{"".join(sections)}</div></main>'
        '<footer><div class="wrap">Created with JT-Code Data Visualization (Apache ECharts) · '
        "Charts are interactive: hover for values, click legend items to hide series, scroll to zoom, "
        "and use the download icon to save an image.</div></footer>"
        f'<script id="charts" type="application/json">{_script_json(charts)}</script>'
        f"<script>(function(){{{register_map}"
        "var items=JSON.parse(document.getElementById('charts').textContent);"
        "items.forEach(function(item){var el=document.getElementById(item.id);"
        "var chart=echarts.init(el,null,{renderer:'canvas'});chart.setOption(window.jtHydrate(item.option));"
        "window.addEventListener('resize',function(){chart.resize();});});})();</script></body></html>"
    )


def decode(payload: dict[str, Any], key: str = "content") -> bytes:
    return base64.b64decode(payload[key])
