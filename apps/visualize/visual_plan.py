"""Dashboard visuals → the aggregate queries they need (shared by the API and the engine).

A *query* is a small, validated description of one aggregation:

    {"groupBy": [{"field": "date", "grain": "month"}, {"field": "region"}],
     "measures": [{"id": "m0", "field": "revenue", "agg": "sum"}],
     "filters": [{"field": "channel", "op": "in", "value": ["Online"]}],
     "limit": 10000}

The API compiles it to SQL against the dataset's Postgres table (``store.py``); the sandboxed
engine runs the same query with pandas when no table is available (``viz_query.py``). Both return
``{"columns": [...], "rows": [[...]], "truncated": bool}``. The engine then turns the results into
finished visuals (``viz_visuals.py``). This module imports nothing from Django.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

AGGREGATIONS = ("sum", "avg", "count", "distinct", "min", "max", "median", "std", "first", "last")
NUMERIC_ONLY = {"sum", "avg", "median", "std"}
ADDITIVE = {"sum", "count"}
GRAINS = ("year", "quarter", "month", "week", "day")
FILTER_OPS = (
    "in",
    "not_in",
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "between",
    "contains",
    "starts_with",
    "ends_with",
    "blank",
    "not_blank",
    "relative",
)
SHOW_VALUE_AS = (
    "none",
    "pct_total",
    "pct_row",
    "pct_column",
    "running_total",
    "diff_prev",
    "pct_diff_prev",
    "rank",
)
VISUAL_TYPES = (
    "card",
    "multi_card",
    "kpi",
    "gauge",
    "bar",
    "column",
    "line",
    "area",
    "combo",
    "donut",
    "pie",
    "treemap",
    "funnel",
    "waterfall",
    "scatter",
    "map",
    "table",
    "matrix",
    "slicer",
    "text",
    "chart",
)
SLICER_STYLES = ("dropdown", "list", "tiles", "between", "date_range", "relative")
MAX_ROWS = 10_000
MAX_FILTERS = 40
MAX_MEASURES = 12
MAX_GROUPS = 4


class PlanError(ValueError):
    """The visual or query does not fit the dataset (unknown field, wrong type, …)."""


def _field(value: Any, kinds: dict[str, str], label: str) -> str:
    if not isinstance(value, str) or value not in kinds:
        raise PlanError(f"Unknown field {value!r} for {label}.")
    return value


def clean_group(item: Any, kinds: dict[str, str]) -> dict[str, Any]:
    raw = item if isinstance(item, dict) else {"field": item}
    field = _field(raw.get("field"), kinds, "a category")
    group: dict[str, Any] = {"field": field}
    grain = raw.get("grain")
    if grain:
        if grain not in GRAINS or kinds[field] != "datetime":
            raise PlanError(f"{field} cannot be grouped by {grain}.")
        group["grain"] = grain
    return group


def clean_measure(item: Any, kinds: dict[str, str], index: int) -> dict[str, Any]:
    raw = item if isinstance(item, dict) else {"field": item}
    field = raw.get("field") or "*"
    agg = raw.get("agg") or ("count" if field == "*" else "sum")
    if agg not in AGGREGATIONS:
        raise PlanError(f"Unknown aggregation {agg!r}.")
    if field == "*":
        agg = "count"
    else:
        _field(field, kinds, "a value")
        if agg in NUMERIC_ONLY and kinds[field] != "numeric":
            agg = "count"  # e.g. “sum of region” → count of rows, like Power BI's default
    measure = {"id": f"m{index}", "field": field, "agg": agg}
    show = raw.get("showValueAs") or "none"
    if show not in SHOW_VALUE_AS:
        raise PlanError(f"Unknown “show value as” {show!r}.")
    if show != "none":
        measure["showValueAs"] = show
    if raw.get("label"):
        measure["label"] = str(raw["label"])[:60]
    return measure


def clean_filter(item: Any, kinds: dict[str, str], today: dt.date) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise PlanError("A filter must be an object.")
    field = _field(item.get("field"), kinds, "a filter")
    op = item.get("op") or "in"
    if op not in FILTER_OPS:
        raise PlanError(f"Unknown filter operator {op!r}.")
    value = item.get("value")
    kind = kinds[field]
    if op == "relative":
        start, end = relative_range(value, today)
        return {"field": field, "op": "between", "value": [start, end]}
    if op in {"blank", "not_blank"}:
        return {"field": field, "op": op}
    if op in {"in", "not_in"}:
        values = value if isinstance(value, list) else [value]
        if len(values) > 500:
            raise PlanError("A filter may list at most 500 values.")
        return {"field": field, "op": op, "value": [_typed(v, kind) for v in values]}
    if op == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise PlanError("“between” needs two values.")
        return {"field": field, "op": op, "value": [_typed(value[0], kind), _typed(value[1], kind)]}
    if op in {"contains", "starts_with", "ends_with"}:
        return {"field": field, "op": op, "value": str(value or "")[:200]}
    return {"field": field, "op": op, "value": _typed(value, kind)}


def _typed(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if kind == "numeric":
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise PlanError(f"{value!r} is not a number.") from exc
    if kind == "datetime":
        try:
            return dt.datetime.fromisoformat(str(value)[:19]).isoformat()
        except ValueError as exc:
            raise PlanError(f"{value!r} is not a date.") from exc
    if kind == "boolean" and isinstance(value, str):
        return value.lower() in {"true", "1", "yes"}
    return str(value)[:500] if not isinstance(value, bool) else value


def relative_range(value: Any, today: dt.date) -> tuple[str, str]:
    """“last 3 months”, “this year”, “next 7 days” → an inclusive ISO date range."""
    spec = value if isinstance(value, dict) else {}
    unit = spec.get("unit", "month")
    mode = spec.get("mode", "last")
    try:
        n = max(1, min(int(spec.get("n", 1)), 1000))
    except TypeError, ValueError:
        n = 1
    if unit not in {"day", "week", "month", "quarter", "year"} or mode not in {"last", "this", "next"}:
        raise PlanError("Relative dates need a unit (day…year) and last/this/next.")

    def start_of(day: dt.date) -> dt.date:
        if unit == "week":
            return day - dt.timedelta(days=day.weekday())
        if unit == "month":
            return day.replace(day=1)
        if unit == "quarter":
            return day.replace(month=(day.month - 1) // 3 * 3 + 1, day=1)
        if unit == "year":
            return day.replace(month=1, day=1)
        return day

    def shift(day: dt.date, count: int) -> dt.date:
        if unit == "day":
            return day + dt.timedelta(days=count)
        if unit == "week":
            return day + dt.timedelta(weeks=count)
        months = count * (3 if unit == "quarter" else 12 if unit == "year" else 1)
        total = day.year * 12 + day.month - 1 + months
        return day.replace(year=total // 12, month=total % 12 + 1, day=1)

    current = start_of(today)
    if mode == "this":
        start, end = current, shift(current, 1)
    elif mode == "last":
        complete = spec.get("complete", True)
        start = shift(current, -n)
        end = current if complete else today + dt.timedelta(days=1)
    else:
        start, end = shift(current, 1), shift(current, 1 + n)
    last = end - dt.timedelta(days=1)
    return start.isoformat(), f"{last.isoformat()}T23:59:59"


def clean_query(raw: dict[str, Any], kinds: dict[str, str], today: dt.date) -> dict[str, Any]:
    groups = [clean_group(g, kinds) for g in (raw.get("groupBy") or [])][:MAX_GROUPS]
    measures = [clean_measure(m, kinds, i) for i, m in enumerate((raw.get("measures") or [])[:MAX_MEASURES])]
    filters = [clean_filter(f, kinds, today) for f in (raw.get("filters") or [])[:MAX_FILTERS]]
    try:
        limit = max(1, min(int(raw.get("limit") or MAX_ROWS), MAX_ROWS))
    except TypeError, ValueError:
        limit = MAX_ROWS
    return {"groupBy": groups, "measures": measures, "filters": filters, "limit": limit}


ROW_LIMIT = 10_000


def chart_columns(fields: Any, kinds: dict[str, str]) -> list[str]:
    """The dataset columns a gallery chart's field mapping uses (validated, in order, unique)."""
    names: list[str] = []
    for value in (fields or {}).values() if isinstance(fields, dict) else []:
        for item in value if isinstance(value, list) else [value]:
            if item in (None, ""):
                continue
            name = _field(item, kinds, "a chart field")
            if name not in names:
                names.append(name)
    return names


def clean_rows(columns: list[str], filters: list[dict[str, Any]], limit: int = ROW_LIMIT) -> dict[str, Any]:
    """A raw-rows query (filtered, evenly sampled when large) for charts that need every point."""
    return {"rows": True, "columns": columns, "filters": filters, "limit": max(1, min(int(limit), ROW_LIMIT))}


def query_key(dataset_token: str, query: dict[str, Any]) -> str:
    """A stable cache key for one query against one version of a dataset."""
    body = json.dumps(query, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{dataset_token}|{body}".encode()).hexdigest()


# --- Visuals → queries ---------------------------------------------------------------------------


def _wells(visual: dict[str, Any]) -> dict[str, list[Any]]:
    data = visual.get("data") if isinstance(visual.get("data"), dict) else {}
    wells = data.get("wells") if isinstance(data.get("wells"), dict) else {}
    return {key: list(value) for key, value in wells.items() if isinstance(value, list)}


def format_section(visual: dict[str, Any], section: str) -> dict[str, Any]:
    fmt = visual.get("format") if isinstance(visual.get("format"), dict) else {}
    value = fmt.get(section)
    return value if isinstance(value, dict) else {}


def _name(ref: Any) -> Any:
    return ref.get("field") if isinstance(ref, dict) else ref


def _first_date(kinds: dict[str, str]) -> str | None:
    return next((name for name, kind in kinds.items() if kind == "datetime"), None)


def plan(
    visual: dict[str, Any], kinds: dict[str, str], filters: list[dict[str, Any]], today: dt.date
) -> dict[str, Any]:
    """The queries a visual needs, keyed by role: ``main`` plus optional extras.

    Extras: ``trend`` (KPI sparkline and period-over-period delta), ``target`` (KPI/gauge goal),
    ``highlight`` (the same data restricted by another visual's selection, drawn on top).
    """
    vtype = visual.get("type")
    if vtype not in VISUAL_TYPES:
        raise PlanError(f"Unknown visual type {vtype!r}.")
    wells = _wells(visual)
    data = visual.get("data") if isinstance(visual.get("data"), dict) else {}
    own = [clean_filter(f, kinds, today) for f in (data.get("filters") or [])[:MAX_FILTERS]]
    where = [*filters, *own]
    values = wells.get("values") or []
    category = wells.get("category") or []
    legend = wells.get("legend") or []
    queries: dict[str, dict[str, Any]] = {}

    def query(
        groups: list[Any], measures: list[Any], extra: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        return clean_query(
            {"groupBy": groups, "measures": measures, "filters": [*where, *(extra or [])]}, kinds, today
        )

    if vtype == "text":
        return {}
    if vtype == "chart":
        chart = format_section(visual, "chart")
        if not chart.get("chartType"):
            raise PlanError("Choose a chart from the gallery.")
        columns = chart_columns(chart.get("fields"), kinds)
        if not columns:
            raise PlanError("Map the chart's columns in the Build pane.")
        queries["main"] = clean_rows(columns, where)
        return queries
    if vtype == "slicer":
        if not category:
            raise PlanError("A slicer needs a field.")
        field = clean_group(category[0], kinds)["field"]
        style = format_section(visual, "slicer").get("style")
        # A slicer never narrows its own options: only the *other* filters apply to it.
        others = [f for f in filters if f.get("field") != field]
        if kinds[field] in {"numeric", "datetime"} and style in {"between", "date_range", "relative", None}:
            measures = [{"field": field, "agg": "min"}, {"field": field, "agg": "max"}]
            queries["main"] = clean_query(
                {"groupBy": [], "measures": measures, "filters": [*others, *own]}, kinds, today
            )
        else:
            queries["main"] = clean_query(
                {
                    "groupBy": [{"field": field}],
                    "measures": [{"field": "*", "agg": "count"}],
                    "filters": [*others, *own],
                },
                kinds,
                today,
            )
        return queries
    if vtype in {"card", "multi_card"}:
        queries["main"] = query([], values or [{"field": "*"}])
        return queries
    if vtype in {"kpi", "gauge"}:
        queries["main"] = query([], values[:1] or [{"field": "*"}])
        target = wells.get("target") or []
        if target:
            queries["target"] = query([], target[:1])
        trend_field = _name(category[0]) if category else (_first_date(kinds) if vtype == "kpi" else None)
        if vtype == "kpi" and trend_field and kinds.get(trend_field) == "datetime":
            grain = format_section(visual, "kpi").get("grain") or "month"
            group = {"field": trend_field, "grain": grain if grain in GRAINS else "month"}
            queries["trend"] = query([group], values[:1] or [{"field": "*"}])
        return queries
    if vtype == "scatter":
        detail = wells.get("details") or category
        measures = (values[:2] + (wells.get("size") or [])[:1]) if len(values) >= 2 else values
        if len(measures) < 2:
            raise PlanError("A scatter chart needs two values (X and Y).")
        queries["main"] = query(detail[:1], measures)
        return queries
    if vtype == "table":
        if not category and not values:
            raise PlanError("Add fields to the table.")
        queries["main"] = query(category[:MAX_GROUPS], values)
        return queries
    if vtype == "matrix":
        rows = wells.get("rows") or category
        columns = wells.get("columns") or legend
        if not rows:
            raise PlanError("A matrix needs row fields.")
        queries["main"] = query([*rows[:2], *columns[:1]], values or [{"field": "*"}])
        return queries
    # Category charts: bar, column, line, area, combo, donut, pie, treemap, funnel, waterfall, map.
    if not category:
        raise PlanError("Add a category (X axis) field.")
    groups = category[:1] + (legend[:1] if vtype in {"bar", "column", "line", "area"} else [])
    measures = values or [{"field": "*"}]
    if vtype == "combo":
        measures = [*(values or [{"field": "*"}])[:2], *(wells.get("secondaryValues") or [])[:2]]
    queries["main"] = query(groups, measures)
    highlight = data.get("highlight") if isinstance(data.get("highlight"), list) else []
    if highlight and vtype in {"bar", "column", "line", "area", "combo", "donut", "pie", "treemap", "funnel"}:
        queries["highlight"] = query(
            groups, measures, [clean_filter(f, kinds, today) for f in highlight[:10]]
        )
    return queries
