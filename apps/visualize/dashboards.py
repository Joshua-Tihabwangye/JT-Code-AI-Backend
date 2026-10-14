"""Answer a batch of dashboard visuals: plan → aggregate (Postgres, else pandas) → build in Python.

Per dataset the flow is:

1. plan every visual's queries against the dataset's column kinds (``visual_plan``);
2. reuse cached query results (Redis, keyed by dataset content + query);
3. run the missing queries in Postgres (``store``) — or, if the dataset's table cannot be built,
   let the engine aggregate them with pandas;
4. reuse cached visuals, otherwise build the rest in ONE sandboxed engine run (``viz_visuals``).

So a page costs at most one engine process per dataset, and an unchanged page none at all.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from typing import Any

from django.conf import settings
from django.core.cache import cache

from apps.analytics.models import Dataset
from apps.analytics.services import AnalysisError
from apps.visualize import services, store, themes, visual_plan

logger = logging.getLogger(__name__)
QUERY_TTL = 600
VISUAL_TTL = 600


def _kinds(dataset: Dataset) -> dict[str, str]:
    profile = services.ensure_profile(dataset)
    return {entry["name"]: entry.get("kind", "categorical") for entry in profile.get("columnProfiles", [])}


def category_order(dataset: Dataset) -> dict[str, list[Any]]:
    """Each column's values in profile order, so a category keeps its colour across visuals."""
    order: dict[str, list[Any]] = {}
    for entry in (dataset.profile or {}).get("columnProfiles", []):
        values = [item.get("value") for item in entry.get("topValues") or [] if isinstance(item, dict)]
        if values:
            order[entry["name"]] = values
    return order


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _filters_for(
    filters: list[dict[str, Any]], dataset: Dataset, kinds: dict[str, str]
) -> list[dict[str, Any]]:
    """Filters apply to a dataset when they target it (``datasetId``) or name one of its fields."""
    return [
        f
        for f in filters
        if (not f.get("datasetId") or f.get("datasetId") == str(dataset.id)) and f.get("field") in kinds
    ]


def render(
    datasets: dict[str, Dataset],
    visuals: list[dict[str, Any]],
    filters: list[dict[str, Any]],
    theme: Any,
    mode: str,
    today: dt.date,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``context`` carries what calculated measures may reference: ``parameters`` (what-if values
    by name) and ``calculations`` (expressions by name)."""
    started = time.monotonic()
    resolved = themes.resolve(theme, mode)
    outputs: dict[str, dict[str, Any]] = {}
    stats = {
        "visuals": len(visuals),
        "cachedVisuals": 0,
        "cachedQueries": 0,
        "sqlQueries": 0,
        "engineRuns": 0,
    }
    by_dataset: dict[str, list[dict[str, Any]]] = {}
    for visual in visuals:
        if visual.get("type") in visual_plan.LOCAL_TYPES:
            # Text, parameter controls, buttons and navigators are drawn by the browser.
            outputs[visual["id"]] = {
                "id": visual["id"],
                "type": visual.get("type"),
                "status": "ok",
                "title": visual.get("title", ""),
            }
            continue
        dataset_id = (visual.get("data") or {}).get("datasetId")
        if dataset_id not in datasets:
            outputs[visual["id"]] = _error(visual, "Choose a dataset for this visual.")
            continue
        by_dataset.setdefault(dataset_id, []).append(visual)
    for dataset_id, group in by_dataset.items():
        dataset = datasets[dataset_id]
        try:
            _render_dataset(dataset, group, filters, resolved, today, outputs, stats, context or {})
        except AnalysisError as exc:
            for visual in group:
                outputs.setdefault(visual["id"], _error(visual, str(exc)))
    stats["ms"] = int((time.monotonic() - started) * 1000)
    return {"visuals": [outputs[v["id"]] for v in visuals if v["id"] in outputs], "meta": stats}


def _error(visual: dict[str, Any], message: str) -> dict[str, Any]:
    return {"id": visual.get("id"), "type": visual.get("type"), "status": "error", "message": message[:300]}


def _render_dataset(
    dataset: Dataset,
    visuals: list[dict[str, Any]],
    filters: list[dict[str, Any]],
    theme: dict[str, Any],
    today: dt.date,
    outputs: dict[str, dict[str, Any]],
    stats: dict[str, int],
    context: dict[str, Any],
) -> None:
    kinds = _kinds(dataset)
    token = store.dataset_token(dataset)
    scoped = _filters_for(filters, dataset, kinds)
    order = category_order(dataset)
    planned: list[tuple[dict[str, Any], dict[str, Any], dict[str, str]]] = []
    for visual in visuals:
        try:
            queries = visual_plan.plan(visual, kinds, scoped, today, context)
        except visual_plan.PlanError as exc:
            outputs[visual["id"]] = _error(visual, str(exc))
            continue
        keys = {role: visual_plan.query_key(token, query) for role, query in queries.items()}
        planned.append((visual, queries, keys))
    theme_key = _digest(theme)
    pending: list[tuple[dict[str, Any], dict[str, Any], dict[str, str], str]] = []
    for visual, queries, keys in planned:
        # The queries are part of the key: a calculation's output changes with its parameters.
        visual_key = "viz-v:" + _digest(visual, keys, queries, theme_key, order)
        cached = cache.get(visual_key)
        if cached is not None:
            outputs[visual["id"]] = cached
            stats["cachedVisuals"] += 1
        else:
            pending.append((visual, queries, keys, visual_key))
    if not pending:
        return
    results: dict[str, Any] = {}
    needed = {
        key: query
        for _, queries, keys, _ in pending
        for role, query in queries.items()
        for key in [keys[role]]
    }
    for key in list(needed):
        hit = cache.get("viz-q:" + key)
        if hit is not None:
            results[key] = hit
            stats["cachedQueries"] += 1
            needed.pop(key)
    record = None
    if needed:
        try:
            record = store.ensure_table(dataset)
        except Exception:  # noqa: BLE001 - the pandas path below still answers the dashboard
            logger.warning(
                "viz query store unavailable for dataset %s; using the engine", dataset.id, exc_info=True
            )
    if record is not None:
        for key, query in needed.items():
            results[key] = store.run_query(record, query)
            cache.set("viz-q:" + key, results[key], QUERY_TTL)
            stats["sqlQueries"] += 1
        needed = {}
    items = [
        {
            "visual": visual,
            "queries": queries,
            "results": {role: results[key] for role, key in keys.items() if key in results},
            "keys": keys,
        }
        for visual, queries, keys, _ in pending
    ]
    # Each Python visual runs in its own engine process, so a slow or failing script only
    # affects its own tile; everything else shares one run.
    batches = [[i for i in items if i["visual"].get("type") != "python"]]
    batches += [[i] for i in items if i["visual"].get("type") == "python"]
    built: dict[Any, dict[str, Any]] = {}
    for batch in batches:
        if not batch:
            continue
        request: dict[str, Any] = {"items": batch, "theme": theme, "categoryOrder": order, "kinds": kinds}
        missing = any(role not in item["results"] for item in batch for role in item["queries"])
        try:
            output = services.engine("viz_visuals", dataset if missing else None, **request)
        except AnalysisError as exc:
            if any(item["visual"].get("type") != "python" for item in batch):
                raise
            output = {"visuals": [_error(batch[0]["visual"], f"The script failed: {exc}")]}
        for key, value in (output.get("results") or {}).items():
            cache.set("viz-q:" + key, value, QUERY_TTL)
        stats["engineRuns"] += 1
        built.update({item.get("id"): item for item in output.get("visuals") or []})
    for visual, _, _, visual_key in pending:
        result = built.get(visual["id"]) or _error(visual, "This visual could not be drawn.")
        outputs[visual["id"]] = result
        if result.get("status") in {"ok", "empty"}:
            cache.set(visual_key, result, VISUAL_TTL)


def max_visuals() -> int:
    return int(getattr(settings, "VISUALIZE_MAX_BATCH_VISUALS", 60))
