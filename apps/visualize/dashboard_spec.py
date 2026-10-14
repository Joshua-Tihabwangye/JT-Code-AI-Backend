"""Validation of dashboard specs (pages → visuals → wells, filters, format) and personal views.

The spec is stored as one JSON document (``Dashboard.spec``). Validation is structural and strict
about size: unknown top-level keys are dropped, ids and types are checked, numbers are clamped, and
every free-form section (format, interactions) is size-capped. Field names are checked later, at
query time, against the dataset's columns (datasets can change after a dashboard is built).
"""

from __future__ import annotations

import json
import re
from typing import Any

from apps.visualize import themes
from apps.visualize.visual_plan import FILTER_OPS, VISUAL_TYPES

SPEC_VERSION = 1
MAX_PAGES = 20
MAX_VISUALS = 60
MAX_SPEC_BYTES = 1_500_000
MAX_FORMAT_BYTES = 20_000
MAX_FILTERS = 40
ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
PAGE_TYPES = ("page", "tooltip", "drillthrough")
CANVAS_PRESETS = ("16:9", "4:3", "letter", "custom", "fit-width")
WELLS = (
    "category",
    "values",
    "legend",
    "secondaryValues",
    "target",
    "size",
    "details",
    "tooltips",
    "rows",
    "columns",
    "smallMultiples",
)


class SpecError(ValueError):
    """The dashboard spec is malformed; the message says where."""


def _id(value: Any, where: str) -> str:
    if not isinstance(value, str) or not ID.match(value):
        raise SpecError(f"{where}: ids are 1–40 letters, digits, '-' or '_'.")
    return value


def _int(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(int(value), high))
    except TypeError, ValueError:
        return default


def _quarter(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except TypeError, ValueError:
        return default
    if number != number:  # NaN
        return default
    rounded = round(max(low, min(number, high)) * 4) / 4
    return int(rounded) if rounded == int(rounded) else rounded


def _text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _json_section(value: Any, where: str, limit: int = MAX_FORMAT_BYTES) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise SpecError(f"{where} must be an object.")
    if len(json.dumps(value, default=str)) > limit:
        raise SpecError(f"{where} is too large.")
    return value


def clean_filter(item: Any, where: str) -> dict[str, Any]:
    if not isinstance(item, dict) or not isinstance(item.get("field"), str):
        raise SpecError(f"{where}: a filter needs a field.")
    op = item.get("op", "in")
    if op not in FILTER_OPS:
        raise SpecError(f"{where}: unknown filter operator {op!r}.")
    clean = {"field": item["field"][:200], "op": op}
    if "value" in item:
        clean["value"] = item["value"]
    for key in ("datasetId", "id", "label"):
        if isinstance(item.get(key), str):
            clean[key] = item[key][:80]
    for key in ("locked", "hidden"):
        if key in item:
            clean[key] = bool(item[key])
    if len(json.dumps(clean, default=str)) > 20_000:
        raise SpecError(f"{where}: the filter is too large.")
    return clean


def _filters(value: Any, where: str) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else []
    if len(items) > MAX_FILTERS:
        raise SpecError(f"{where}: at most {MAX_FILTERS} filters.")
    return [clean_filter(item, where) for item in items]


def _ref(item: Any, where: str) -> dict[str, Any]:
    raw = item if isinstance(item, dict) else {"field": item}
    if not isinstance(raw.get("field"), str):
        raise SpecError(f"{where}: a well item needs a field.")
    clean = {"field": raw["field"][:200]}
    for key in ("grain", "agg", "showValueAs", "label", "param"):
        if isinstance(raw.get(key), str):
            clean[key] = raw[key][:60]
    return clean


def clean_visual(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SpecError(f"{where} must be an object.")
    vid = _id(raw.get("id"), where)
    vtype = raw.get("type")
    if vtype not in VISUAL_TYPES:
        raise SpecError(f"{where}: unknown visual type {vtype!r}.")
    layout = raw.get("layout") if isinstance(raw.get("layout"), dict) else {}
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    wells_in = data.get("wells") if isinstance(data.get("wells"), dict) else {}
    wells = {}
    for key in WELLS:
        items = wells_in.get(key)
        if isinstance(items, list):
            if len(items) > 12:
                raise SpecError(f"{where}: at most 12 fields per well.")
            wells[key] = [_ref(item, f"{where} {key}") for item in items]
    visual: dict[str, Any] = {
        "id": vid,
        "type": vtype,
        "title": _text(raw.get("title"), 120),
        "layout": {
            # Grid units; free positioning snaps to quarter units.
            "x": _quarter(layout.get("x"), 0, 2000, 0),
            "y": _quarter(layout.get("y"), 0, 5000, 0),
            "w": _quarter(layout.get("w"), 0.5, 2000, 6),
            "h": _quarter(layout.get("h"), 0.5, 2000, 6),
            "z": _int(layout.get("z"), 0, 1000, 0),
        },
        "data": {
            "datasetId": _text(data.get("datasetId"), 40),
            "wells": wells,
            "filters": _filters(data.get("filters"), f"{where} filters"),
            # Another visual's selection, drawn over this one (cross-highlight).
            "highlight": _filters(data.get("highlight"), f"{where} highlight"),
        },
        "format": _json_section(raw.get("format"), f"{where} format"),
        "interactions": _json_section(raw.get("interactions"), f"{where} interactions", 4_000),
        "hidden": bool(raw.get("hidden", False)),
        "locked": bool(raw.get("locked", False)),
        "allowPersonalize": bool(raw.get("allowPersonalize", True)),
    }
    if isinstance(raw.get("groupId"), str) and raw["groupId"]:
        visual["groupId"] = _id(raw["groupId"], f"{where} group")
    if vtype == "text":
        visual["text"] = _text(raw.get("text"), 4_000)
    if isinstance(raw.get("altText"), str):
        visual["altText"] = raw["altText"][:300]
    return visual


def clean_page(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SpecError(f"{where} must be an object.")
    visuals_in = raw.get("visuals") if isinstance(raw.get("visuals"), list) else []
    if len(visuals_in) > MAX_VISUALS:
        raise SpecError(f"{where}: at most {MAX_VISUALS} visuals per page.")
    visuals = [clean_visual(v, f"{where} visual {i + 1}") for i, v in enumerate(visuals_in)]
    ids = [v["id"] for v in visuals]
    if len(set(ids)) != len(ids):
        raise SpecError(f"{where}: visual ids must be unique.")
    canvas = raw.get("canvas") if isinstance(raw.get("canvas"), dict) else {}
    page_type = raw.get("type", "page")
    return {
        "id": _id(raw.get("id"), where),
        "name": _text(raw.get("name") or "Page", 60),
        "hidden": bool(raw.get("hidden", False)),
        "type": page_type if page_type in PAGE_TYPES else "page",
        "canvas": {
            "preset": canvas.get("preset") if canvas.get("preset") in CANVAS_PRESETS else "16:9",
            "width": _int(canvas.get("width"), 320, 4000, 1280),
            "height": _int(canvas.get("height"), 240, 8000, 720),
            "background": _text(canvas.get("background"), 40),
        },
        "filters": _filters(raw.get("filters"), f"{where} filters"),
        "visuals": visuals,
        "mobileLayout": [item for item in (raw.get("mobileLayout") or []) if isinstance(item, dict)][
            :MAX_VISUALS
        ],
    }


def clean_spec(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SpecError("The dashboard spec must be an object.")
    if len(json.dumps(raw, default=str)) > MAX_SPEC_BYTES:
        raise SpecError("The dashboard is too large.")
    pages_in = raw.get("pages") if isinstance(raw.get("pages"), list) else []
    if not pages_in:
        pages_in = [{"id": "page-1", "name": "Overview", "visuals": []}]
    if len(pages_in) > MAX_PAGES:
        raise SpecError(f"A dashboard may have at most {MAX_PAGES} pages.")
    pages = [clean_page(page, f"Page {i + 1}") for i, page in enumerate(pages_in)]
    if len({p["id"] for p in pages}) != len(pages):
        raise SpecError("Page ids must be unique.")
    theme = raw.get("theme", themes.DEFAULT_THEME)
    if isinstance(theme, dict):
        theme = themes.clean_theme(theme)
    elif theme not in themes.BUILT_IN:
        theme = themes.DEFAULT_THEME
    settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    return {
        "version": SPEC_VERSION,
        "theme": theme,
        "mode": raw.get("mode") if raw.get("mode") in {"light", "dark", "system"} else "system",
        "density": raw.get("density") if raw.get("density") in themes.DENSITIES else "comfortable",
        "settings": {
            "allowPersonalize": bool(settings.get("allowPersonalize", True)),
            "showFiltersPane": bool(settings.get("showFiltersPane", True)),
            "lockFilters": bool(settings.get("lockFilters", False)),
            "defaultPageId": _text(settings.get("defaultPageId"), 40),
        },
        "filters": _filters(raw.get("filters"), "Dashboard filters"),
        "parameters": _json_list(raw.get("parameters"), "Parameters"),
        "calculations": _json_list(raw.get("calculations"), "Calculations"),
        "bookmarks": _json_list(raw.get("bookmarks"), "Bookmarks"),
        "syncGroups": _json_list(raw.get("syncGroups"), "Sync groups"),
        "pages": pages,
    }


def _json_list(value: Any, where: str, limit: int = 100) -> list[Any]:
    items = value if isinstance(value, list) else []
    if len(items) > limit:
        raise SpecError(f"{where}: at most {limit} items.")
    if len(json.dumps(items, default=str)) > 200_000:
        raise SpecError(f"{where} are too large.")
    return [item for item in items if isinstance(item, dict)]


def dataset_ids(spec: dict[str, Any]) -> set[str]:
    return {
        visual["data"]["datasetId"]
        for page in spec.get("pages", [])
        for visual in page.get("visuals", [])
        if visual.get("data", {}).get("datasetId")
    }


def clean_personal_state(raw: Any) -> dict[str, Any]:
    """A viewer's overrides: per-visual changes, slicer/filter state and display choices."""
    if not isinstance(raw, dict):
        raise SpecError("A personal view must be an object.")
    if len(json.dumps(raw, default=str)) > 300_000:
        raise SpecError("The personal view is too large.")
    overrides = raw.get("visualOverrides") if isinstance(raw.get("visualOverrides"), dict) else {}
    clean_overrides = {}
    for vid, override in list(overrides.items())[: MAX_PAGES * MAX_VISUALS]:
        if isinstance(vid, str) and ID.match(vid) and isinstance(override, dict):
            clean_overrides[vid] = {
                key: override[key] for key in ("type", "data", "format", "hidden") if key in override
            }
    return {
        "visualOverrides": clean_overrides,
        "slicers": raw.get("slicers") if isinstance(raw.get("slicers"), dict) else {},
        "filters": _filters(raw.get("filters"), "Personal filters"),
        "pageId": _text(raw.get("pageId"), 40),
        "mode": raw.get("mode") if raw.get("mode") in {"light", "dark", "system"} else None,
        "density": raw.get("density") if raw.get("density") in themes.DENSITIES else None,
    }
