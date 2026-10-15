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

from apps.visualize import viz_expr

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
    "or",
    "and",
    "top",
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
    "moving_avg",
    "yoy_pct",
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
    "python",
    # Drawn in the browser, no query: parameter controls, buttons and page/bookmark navigators.
    "parameter",
    "button",
    "navigator",
    # A frozen chart (e.g. a prediction pinned from Predict), drawn from its stored option.
    "snapshot",
)
LOCAL_TYPES = frozenset({"text", "parameter", "button", "navigator", "snapshot"})
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
    _extras(raw, measure)
    return measure


def _extras(raw: dict[str, Any], measure: dict[str, Any]) -> None:
    if measure.get("showValueAs") == "moving_avg":
        try:
            measure["window"] = max(2, min(int(raw.get("window") or 3), 60))
        except TypeError, ValueError:
            measure["window"] = 3
    if raw.get("format") in {"number", "percent", "currency"}:
        measure["format"] = raw["format"]


NUMERIC_LEAVES = {"sum", "avg", "min", "max", "median"}


def calc_measure(
    raw: dict[str, Any], kinds: dict[str, str], index: int, context: dict[str, Any] | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A calculated measure → its output description and the plain measures (leaves) it needs."""
    context = context or {}
    try:
        tree = viz_expr.resolve(
            viz_expr.parse(raw.get("expr")), context.get("parameters"), context.get("calculations")
        )
        leaves = viz_expr.aggregates(tree)
    except viz_expr.ExprError as exc:
        name = raw.get("label") or raw.get("field") or "calculation"
        raise PlanError(f"{name}: {exc}") from exc
    physical: list[dict[str, Any]] = []
    mapping: list[list[str]] = []
    for position, (agg, field) in enumerate(leaves):
        if field != "*":
            _field(field, kinds, "a calculation")
            if agg in NUMERIC_LEAVES and kinds[field] != "numeric":
                raise PlanError(f"{agg.upper()}([{field}]) needs a numeric field.")
        leaf = {"id": f"m{index}_{position}", "field": field, "agg": agg}
        physical.append(leaf)
        mapping.append([agg, field, leaf["id"]])
    label = str(raw.get("label") or raw.get("field") or "Calculation")[:60]
    measure: dict[str, Any] = {
        "id": f"m{index}",
        "field": label,
        "agg": "calc",
        "label": label,
        "expr": tree,
        "leaves": mapping,
    }
    show = raw.get("showValueAs") or "none"
    if show not in SHOW_VALUE_AS:
        raise PlanError(f"Unknown “show value as” {show!r}.")
    if show != "none":
        measure["showValueAs"] = show
    _extras(raw, measure)
    return measure, physical


def clean_filter(item: Any, kinds: dict[str, str], today: dt.date) -> dict[str, Any] | None:
    """A validated filter, or ``None`` when it filters nothing (an empty value list)."""
    if not isinstance(item, dict):
        raise PlanError("A filter must be an object.")
    op = item.get("op") or "in"
    if op not in FILTER_OPS:
        raise PlanError(f"Unknown filter operator {op!r}.")
    value = item.get("value")
    if op in {"or", "and"}:
        # Advanced filter: any (or all) of up to five conditions (no nesting).
        parts = [
            p
            for p in (value if isinstance(value, list) else [])
            if isinstance(p, dict) and p.get("op") not in {"or", "and"}
        ]
        if not 1 <= len(parts) <= 5:
            raise PlanError(f"“{op}” needs one to five conditions.")
        cleaned = [c for c in (clean_filter(part, kinds, today) for part in parts) if c]
        if not cleaned:
            return None
        return {"field": cleaned[0]["field"], "op": op, "value": cleaned}
    field = _field(item.get("field"), kinds, "a filter")
    kind = kinds[field]
    if op == "top":
        spec = value if isinstance(value, dict) else {}
        by = spec.get("by") if isinstance(spec.get("by"), dict) else {"field": "*"}
        measure = clean_measure(by, kinds, 0)
        try:
            n = max(1, min(int(spec.get("n", 10)), 1000))
        except (TypeError, ValueError) as exc:
            raise PlanError("Top N needs a number.") from exc
        direction = "bottom" if spec.get("direction") == "bottom" else "top"
        return {
            "field": field,
            "op": "top",
            "value": {
                "n": n,
                "by": {"field": measure["field"], "agg": measure["agg"]},
                "direction": direction,
            },
        }
    if op == "relative":
        start, end = relative_range(value, today)
        return {"field": field, "op": "between", "value": [start, end]}
    if op in {"blank", "not_blank"}:
        return {"field": field, "op": op}
    if op in {"in", "not_in"}:
        values = value if isinstance(value, list) else [value]
        if not values:
            return None
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


def _filters(items: Any, kinds: dict[str, str], today: dt.date) -> list[dict[str, Any]]:
    cleaned = (clean_filter(item, kinds, today) for item in (items or [])[:MAX_FILTERS])
    return [item for item in cleaned if item]


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


def clean_query(
    raw: dict[str, Any], kinds: dict[str, str], today: dt.date, context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A validated query. Calculated measures (``expr``) become plain aggregate leaves in
    ``measures`` (what Postgres or pandas computes) plus an ``output`` list (what the visual shows),
    which the engine evaluates over the aggregated rows."""
    groups = [clean_group(g, kinds) for g in (raw.get("groupBy") or [])][:MAX_GROUPS]
    physical: list[dict[str, Any]] = []
    output: list[dict[str, Any]] = []
    for index, item in enumerate((raw.get("measures") or [])[:MAX_MEASURES]):
        if isinstance(item, dict) and item.get("expr"):
            measure, leaves = calc_measure(item, kinds, index, context)
            physical.extend(leaves)
        else:
            measure = clean_measure(item, kinds, index)
            physical.append(measure)
        output.append(measure)
    filters = _filters(raw.get("filters"), kinds, today)
    try:
        limit = max(1, min(int(raw.get("limit") or MAX_ROWS), MAX_ROWS))
    except TypeError, ValueError:
        limit = MAX_ROWS
    derived = any(m["agg"] == "calc" for m in output)
    if derived and not physical:
        # Only constants and parameters: still one row per group.
        physical.append({"id": "n", "field": "*", "agg": "count"})
    query: dict[str, Any] = {"groupBy": groups, "measures": physical, "filters": filters, "limit": limit}
    if derived:
        query["output"] = output
    return query


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
    """A stable cache key for one query against one version of a dataset (the database part only:
    a calculation's ``output`` is computed afterwards, so changing a parameter reuses the result)."""
    physical = {key: value for key, value in query.items() if key != "output"}
    body = json.dumps(physical, sort_keys=True, separators=(",", ":"), default=str)
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
    visual: dict[str, Any],
    kinds: dict[str, str],
    filters: list[dict[str, Any]],
    today: dt.date,
    context: dict[str, Any] | None = None,
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
    own = _filters(data.get("filters"), kinds, today)
    where = [*filters, *own]
    values = wells.get("values") or []
    category = wells.get("category") or []
    legend = wells.get("legend") or []
    queries: dict[str, dict[str, Any]] = {}

    def query(
        groups: list[Any], measures: list[Any], extra: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        return clean_query(
            {"groupBy": groups, "measures": measures, "filters": [*where, *(extra or [])]},
            kinds,
            today,
            context,
        )

    if vtype in LOCAL_TYPES:
        return {}
    if vtype == "python":
        columns = [_field(_name(ref), kinds, "a Python visual") for ref in values]
        if not columns:
            raise PlanError("Add the fields your script uses to the Values well.")
        queries["main"] = clean_rows(list(dict.fromkeys(columns)), where)
        return queries
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
        if style == "hierarchy" and len(category) > 1:
            fields = [clean_group({"field": _name(ref)}, kinds) for ref in category[:3]]
            # Hierarchy slicers ignore filters on any of their own levels.
            mine = {g["field"] for g in fields}
            others = [f for f in filters if f.get("field") not in mine]
            queries["main"] = clean_query(
                {"groupBy": fields, "measures": [{"field": "*", "agg": "count"}], "filters": [*others, *own]},
                kinds,
                today,
            )
            return queries
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
    multiples = wells.get("smallMultiples") or []
    if multiples and vtype in {"bar", "column", "line", "area"}:
        # Small multiples: one panel per member of the second group (no legend).
        queries["main"] = query(category[:1] + multiples[:1], (values or [{"field": "*"}])[:1])
        return queries
    groups = category[:1] + (legend[:1] if vtype in {"bar", "column", "line", "area"} else [])
    measures = values or [{"field": "*"}]
    if vtype == "combo":
        measures = [*(values or [{"field": "*"}])[:2], *(wells.get("secondaryValues") or [])[:2]]
    queries["main"] = query(groups, measures)
    highlight = data.get("highlight") if isinstance(data.get("highlight"), list) else []
    if highlight and vtype in {"bar", "column", "line", "area", "combo", "donut", "pie", "treemap", "funnel"}:
        queries["highlight"] = query(groups, measures, _filters(highlight[:10], kinds, today))
    return queries
