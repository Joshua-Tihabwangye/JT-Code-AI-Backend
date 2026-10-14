"""Dashboard visuals built in Python from aggregated query results (engine side).

``build(visual, queries, results, ctx)`` returns what the browser displays:

* ``option`` — an Apache ECharts option (bar, column, line, area, combo, donut/pie, treemap, funnel,
  waterfall, scatter, gauge, map), with ``$fn`` markers for formatters;
* ``kpi`` / ``cards`` — values, deltas, targets and sparklines for KPI and card tiles;
* ``table`` — rows for tables and matrices (with colour scales computed here);
* ``slicer`` — the options or range a slicer offers;
* ``data`` — the visual's numbers as a table (“show as table”, export) and ``selectors`` that map
  every category and series back to the raw value, so a click can filter other visuals.

The chart defaults follow the dashboard design rules: bars fill 55–70 % of their band (max 48 px),
categorical bars sorted by value, dates chronological, more than 12 categories → Top N + Others,
data labels only when few marks, horizontal light gridlines, legends only with several series,
donuts ≤ 6 slices with outside “Name · %” labels, and the same colour for a category everywhere.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd

_HERE = Path(__file__).resolve().parent
if "jt_viz_query" not in sys.modules:  # loaded by path inside the sandbox
    import importlib.util

    _spec = importlib.util.spec_from_file_location("jt_viz_query", _HERE / "viz_query.py")
    assert _spec and _spec.loader
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["jt_viz_query"] = _module
    _spec.loader.exec_module(_module)
_q = sys.modules["jt_viz_query"]
BLANK, frame, show_value_as, top_n = _q.BLANK, _q.frame, _q.show_value_as, _q.top_n

AGG_LABEL = {
    "sum": "Total {}",
    "avg": "Average {}",
    "count": "Count of {}",
    "distinct": "Distinct {}",
    "min": "Min {}",
    "max": "Max {}",
    "median": "Median {}",
    "std": "Std dev of {}",
    "first": "First {}",
    "last": "Last {}",
}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
TOP_N = 12
MAX_SLICES = 6


def fn(name: str, **arguments: Any) -> dict[str, Any]:
    return {"$fn": name, **arguments}


def compact(value: Any, percent: bool = False) -> str:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return "—"
    number = float(value)
    if percent:
        return f"{number * 100:.1f}%"
    magnitude = abs(number)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if magnitude >= limit:
            text = f"{number / limit:.2f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    if magnitude >= 100 or number == int(number):
        return f"{number:,.0f}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def nice(name: Any) -> str:
    import re

    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z][a-z])|(?<=[a-z])(?=[A-Z])", " ", str(name))
    text = re.sub(r"[_\s]+", " ", text).strip()
    words = [w.upper() if w.lower() in {"id", "kpi", "sku", "iso", "url"} else w for w in text.split(" ")]
    out = " ".join(words)
    return out[:1].upper() + out[1:] if out else str(name)


def measure_label(measure: dict[str, Any]) -> str:
    if measure.get("label"):
        return str(measure["label"])
    if measure["field"] == "*":
        return "Count"
    name = nice(measure["field"])
    return (
        AGG_LABEL.get(measure["agg"], "{}").format(name.lower() if measure["agg"] != "sum" else name).strip()
    )


def period_label(value: Any, grain: str | None) -> str:
    if value is None or value == BLANK:
        return BLANK
    if not grain:
        return str(value)
    try:
        moment = dt.datetime.fromisoformat(str(value)[:19])
    except ValueError:
        return str(value)
    if grain == "year":
        return str(moment.year)
    if grain == "quarter":
        return f"Q{(moment.month - 1) // 3 + 1} {moment.year}"
    if grain == "month":
        return f"{MONTHS[moment.month - 1]} {moment.year}"
    return f"{moment.day} {MONTHS[moment.month - 1]} {moment.year}"


def period_bounds(value: Any, grain: str) -> tuple[str, str]:
    """The first and last moment of the period that starts at ``value`` (for click-to-filter)."""
    start = pd.Timestamp(str(value)[:19])
    if grain == "year":
        end = start + pd.DateOffset(years=1)
    elif grain == "quarter":
        end = start + pd.DateOffset(months=3)
    elif grain == "month":
        end = start + pd.DateOffset(months=1)
    elif grain == "week":
        end = start + pd.Timedelta(days=7)
    else:
        end = start + pd.Timedelta(days=1)
    return start.isoformat(), (end - pd.Timedelta(seconds=1)).isoformat()


class Ctx:
    """Theme, formatting and colour context for one batch of visuals."""

    def __init__(self, theme: dict[str, Any], category_order: dict[str, list[Any]] | None = None) -> None:
        self.theme = theme
        self.colors: list[str] = list(theme["dataColors"])
        self.s: dict[str, str] = theme["structural"]
        self.font = theme.get("fontFamily", "Inter")
        self.order = category_order or {}

    def color(self, index: int) -> str:
        return self.colors[index % len(self.colors)]

    def with_colors(self, colors: list[str]) -> Ctx:
        """A copy whose palette starts with the visual's own colours (Format pane → Colours)."""
        copy = Ctx(self.theme, self.order)
        copy.colors = colors + [c for c in self.colors if c not in colors]
        return copy

    def category_color(self, field: str | None, value: Any) -> str:
        """The same category gets the same colour in every visual of a dashboard."""
        values = [str(v) for v in self.order.get(field or "", [])]
        text = str(value)
        if text in values:
            return self.color(values.index(text))
        if text == "Others":
            return self.theme.get("neutral", "#94A3B8")
        return self.color(sum(ord(ch) for ch in text) % max(len(self.colors), 1))


def _fmt(visual: dict[str, Any], section: str) -> dict[str, Any]:
    fmt = visual.get("format") if isinstance(visual.get("format"), dict) else {}
    value = fmt.get(section)
    return value if isinstance(value, dict) else {}


def _title(visual: dict[str, Any], default: str) -> str:
    title = _fmt(visual, "title")
    return str(title.get("text") or visual.get("title") or default)[:120]


def _base(ctx: Ctx, *, legend: bool, axis: bool = True) -> dict[str, Any]:
    option: dict[str, Any] = {
        "backgroundColor": "transparent",
        "color": ctx.colors,
        "textStyle": {"fontFamily": ctx.font, "color": ctx.s["text"]},
        "animationDuration": 400,
        "tooltip": {
            "trigger": "axis" if axis else "item",
            "confine": True,
            "backgroundColor": ctx.s["tile"],
            "borderColor": ctx.s["border"],
            "textStyle": {"color": ctx.s["text"], "fontSize": 12},
            "valueFormatter": fn("number"),
        },
    }
    if legend:
        option["legend"] = {
            "type": "scroll",
            "top": 0,
            "right": 0,
            "icon": "roundRect",
            "itemWidth": 10,
            "itemHeight": 10,
            "textStyle": {"color": ctx.s["mutedText"], "fontSize": 11},
        }
    if axis:
        option["grid"] = {
            "left": 8,
            "right": 12,
            "top": 30 if legend else 12,
            "bottom": 8,
            "containLabel": True,
        }
    return option


def _value_axis(ctx: Ctx, name: str = "", percent: bool = False) -> dict[str, Any]:
    return {
        "type": "value",
        "name": name,
        "nameTextStyle": {"color": ctx.s["mutedText"], "fontSize": 11},
        "axisLabel": {
            "color": ctx.s["mutedText"],
            "fontSize": 11,
            "formatter": fn("percent", scale=100, digits=0) if percent else fn("compact"),
        },
        "splitNumber": 4,
        "splitLine": {"lineStyle": {"color": ctx.s["gridline"]}},
        "axisLine": {"show": False},
        "axisTick": {"show": False},
    }


def _category_axis(ctx: Ctx, labels: list[str], *, rotate: bool = False) -> dict[str, Any]:
    return {
        "type": "category",
        "data": labels,
        "axisLabel": {
            "color": ctx.s["mutedText"],
            "fontSize": 11,
            "interval": 0 if len(labels) <= 24 else "auto",
            "rotate": 30 if rotate else 0,
            "width": 110,
            "overflow": "truncate",
        },
        "axisLine": {"lineStyle": {"color": ctx.s["border"]}},
        "axisTick": {"show": False},
        "splitLine": {"show": False},
    }


def _empty(visual: dict[str, Any], message: str = "No data for the current filters") -> dict[str, Any]:
    return {"id": visual.get("id"), "type": visual.get("type"), "status": "empty", "message": message}


# --- Shared result shaping -----------------------------------------------------------------------


def _shape(visual, query, result, ctx, *, sort_by_value=True, top=True):
    """Result → (table, category column, legend column or None, measures, notes, selectors)."""
    table = frame(result)
    groups = query["groupBy"]
    measures = query["measures"]
    notes: list[str] = []
    if table.empty:
        return table, None, None, measures, notes
    cat = "g0" if groups else None
    leg = "g1" if len(groups) > 1 else None
    grain = groups[0].get("grain") if groups else None
    for measure in measures:
        if measure.get("showValueAs"):
            table[measure["id"]] = show_value_as(table, measure["id"], measure["showValueAs"], cat, leg)
    first = measures[0]["id"] if measures else None
    sort = _fmt(visual, "sort")
    if cat and first:
        if grain:
            table = table.sort_values(cat, kind="stable")
        elif sort.get("by") == "category":
            table = table.sort_values(cat, ascending=sort.get("dir") != "desc", kind="stable")
        elif sort_by_value:
            order = table.groupby(cat)[first].sum().sort_values(ascending=sort.get("dir") == "asc").index
            table[cat] = pd.Categorical(table[cat], categories=list(order), ordered=True)
            table = table.sort_values(cat, kind="stable")
            table[cat] = table[cat].astype(object)
        topn = _fmt(visual, "topN")
        limit = int(topn.get("n", TOP_N)) if topn.get("enabled", True) else 0
        if top and limit and not grain:
            additive = all(m["agg"] in {"sum", "count"} and not m.get("showValueAs") for m in measures)
            table, cut = top_n(table, cat, first, limit, topn.get("others", True), additive)
            if cut:
                notes.append(f"Top {limit}" + (" + Others" if additive and topn.get("others", True) else ""))
    if leg and first:
        legend_limit = 8
        if table[leg].nunique() > legend_limit:
            additive = all(m["agg"] in {"sum", "count"} for m in measures)
            swapped = table.rename(columns={cat: "_c", leg: cat})
            swapped, _ = top_n(swapped, cat, first, legend_limit, True, additive)
            table = swapped.rename(columns={cat: leg, "_c": cat})
            notes.append(f"Top {legend_limit} series")
    return table, cat, leg, measures, notes


def _labels(values: list[Any], grain: str | None) -> list[str]:
    return [period_label(v, grain) if grain else (BLANK if v is None else str(v)) for v in values]


def _selectors(field: str | None, values: list[Any], grain: str | None) -> list[dict[str, Any] | None]:
    if not field:
        return []
    out: list[dict[str, Any] | None] = []
    for value in values:
        if value == "Others":
            out.append(None)
        elif value == BLANK:
            out.append({"field": field, "op": "blank"})
        elif grain:
            start, end = period_bounds(value, grain)
            out.append({"field": field, "op": "between", "value": [start, end], "grain": grain})
        else:
            out.append({"field": field, "op": "in", "value": [value]})
    return out


def _data(columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    return {"columns": columns, "rows": rows[:500], "truncated": len(rows) > 500}


def _pct(measures: list[dict[str, Any]]) -> bool:
    return bool(measures) and measures[0].get("showValueAs") in {
        "pct_total",
        "pct_row",
        "pct_column",
        "pct_diff_prev",
    }


# --- Builders -----------------------------------------------------------------------------------


def build_cards(visual, queries, results, ctx):
    query, result = queries["main"], results["main"]
    row = (result.get("rows") or [[None] * len(query["measures"])])[0]
    cards = [
        {"label": measure_label(m), "value": value, "display": compact(value)}
        for m, value in zip(query["measures"], row, strict=False)
    ]
    out = {
        "cards": cards,
        "data": _data([c["label"] for c in cards], [[c["value"] for c in cards]]),
        "autoTitle": ", ".join(
            nice(m["field"]) if m["field"] != "*" else "Count" for m in query["measures"][:3]
        ),
    }
    if visual["type"] == "card":
        out["kpi"] = {**cards[0], "label": _title(visual, cards[0]["label"])}
    return out


def build_kpi(visual, queries, results, ctx):
    query = queries["main"]
    measure = query["measures"][0]
    value = (results["main"].get("rows") or [[None]])[0][0]
    fmt = _fmt(visual, "kpi")
    up_is_good = fmt.get("upIsGood", True)
    kpi: dict[str, Any] = {
        "label": _title(visual, measure_label(measure)),
        "value": value,
        "display": compact(value),
    }
    trend = results.get("trend")
    if trend and trend.get("rows"):
        points = [(row[0], row[1]) for row in trend["rows"] if row[0] is not None and row[1] is not None]
        if points:
            grain = queries["trend"]["groupBy"][0].get("grain")
            kpi["sparkline"] = [p[1] for p in points[-12:]]
            kpi["sparkLabels"] = [period_label(p[0], grain) for p in points[-12:]]
            if len(points) >= 2 and points[-2][1]:
                current, previous = float(points[-1][1]), float(points[-2][1])
                change = (current - previous) / abs(previous)
                direction = "up" if change > 0.0005 else "down" if change < -0.0005 else "flat"
                arrow = {"up": "▲", "down": "▼", "flat": "■"}[direction]
                kpi["delta"] = {
                    "value": change,
                    "display": f"{arrow} {abs(change) * 100:.1f}%",
                    "direction": direction,
                    "good": direction == "flat" or (direction == "up") == up_is_good,
                    "label": f"vs prev. {grain or 'period'}",
                    "period": period_label(points[-1][0], grain),
                }
    target_result = results.get("target")
    if target_result and target_result.get("rows"):
        target = target_result["rows"][0][0]
        if target:
            progress = float(value or 0) / float(target)
            kpi["target"] = {
                "value": target,
                "display": compact(target),
                "progress": progress,
                "progressDisplay": f"{progress * 100:.0f}%",
            }
    return {"kpi": kpi, "data": _data([kpi["label"]], [[value]])}


def build_gauge(visual, queries, results, ctx):
    measure = queries["main"]["measures"][0]
    value = (results["main"].get("rows") or [[None]])[0][0]
    target = None
    if results.get("target") and results["target"].get("rows"):
        target = results["target"]["rows"][0][0]
    fmt = _fmt(visual, "gauge")
    maximum = float(fmt.get("max") or (float(target) * 1.25 if target else (float(value or 0) * 1.5 or 1)))
    option = _base(ctx, legend=False, axis=False)
    option["series"] = [
        {
            "type": "gauge",
            "startAngle": 200,
            "endAngle": -20,
            "min": float(fmt.get("min") or 0),
            "max": maximum,
            "radius": "100%",
            "center": ["50%", "62%"],
            "progress": {"show": True, "width": 14, "roundCap": True, "itemStyle": {"color": ctx.color(0)}},
            "axisLine": {"roundCap": True, "lineStyle": {"width": 14, "color": [[1, ctx.s["gridline"]]]}},
            "axisTick": {"show": False},
            "splitLine": {"show": False},
            "axisLabel": {"show": False},
            "pointer": {"show": False},
            "anchor": {"show": False},
            "title": {"show": True, "offsetCenter": [0, "28%"], "color": ctx.s["mutedText"], "fontSize": 12},
            "detail": {
                "valueAnimation": True,
                "offsetCenter": [0, "-5%"],
                "fontSize": 26,
                "fontWeight": 700,
                "color": ctx.s["text"],
                "formatter": fn("compact"),
            },
            "data": [
                {"value": value, "name": f"Target {compact(target)}" if target else measure_label(measure)}
            ],
        }
    ]
    return {
        "option": option,
        "data": _data([measure_label(measure), "Target"], [[value, target]]),
        "autoTitle": f"{measure_label(measure)} vs target" if target else measure_label(measure),
    }


def build_category(visual, queries, results, ctx):
    """bar, column, line, area and combo."""
    vtype = visual["type"]
    query = queries["main"]
    table, cat, leg, measures, notes = _shape(
        visual, query, results["main"], ctx, sort_by_value=vtype in {"bar", "column"}
    )
    if table.empty or not cat:
        return _empty(visual)
    grain = query["groupBy"][0].get("grain")
    category_field = query["groupBy"][0]["field"]
    raw_categories = list(dict.fromkeys(table[cat].tolist()))
    labels = _labels(raw_categories, grain)
    fmt_bar = _fmt(visual, "bar")
    fmt_labels = _fmt(visual, "labels")
    stack = fmt_bar.get("stack", "none")
    percent = _pct(measures) or stack == "percent"
    horizontal = vtype == "bar" or (
        vtype == "column"
        and fmt_bar.get("autoHorizontal", True)
        and len(labels) > 3
        and sum(len(x) for x in labels) / len(labels) > 14
    )
    series: list[dict[str, Any]] = []
    series_selectors: list[dict[str, Any] | None] = []
    if leg:
        legend_field = query["groupBy"][1]["field"]
        pivot = table.pivot_table(index=cat, columns=leg, values=measures[0]["id"], aggfunc="sum", sort=False)
        pivot = pivot.reindex(raw_categories)
        if stack == "percent":
            pivot = pivot.div(pivot.sum(axis=1).replace(0, float("nan")), axis=0)
        for name in pivot.columns:
            series.append(
                {
                    "name": str(name),
                    "values": pivot[name].tolist(),
                    "color": ctx.category_color(legend_field, name),
                }
            )
            series_selectors.append(_selectors(legend_field, [name], None)[0])
    else:
        secondary_ids = {m["id"] for m in measures[2:]} if vtype == "combo" else set()
        for index, measure in enumerate(measures):
            values = table.set_index(cat).reindex(raw_categories)[measure["id"]].tolist()
            series.append(
                {
                    "name": measure_label(measure),
                    "values": values,
                    "color": ctx.color(index),
                    "secondary": measure["id"] in secondary_ids
                    or (
                        vtype == "combo"
                        and index == len(measures) - 1
                        and len(measures) > 1
                        and not secondary_ids
                    ),
                }
            )
            series_selectors.append(None)
    few = len(labels) <= 8 and len(series) == 1
    show_labels = fmt_labels.get("show", few if vtype in {"bar", "column", "combo"} else len(labels) <= 6)
    option = _base(ctx, legend=len(series) > 1 and _fmt(visual, "legend").get("show", True) is not False)
    category_axis = _category_axis(ctx, labels, rotate=not horizontal and len(labels) > 8)
    value_axis = _value_axis(ctx, percent=percent)
    if horizontal:
        category_axis["inverse"] = True
        option["xAxis"], option["yAxis"] = value_axis, category_axis
    else:
        option["xAxis"], option["yAxis"] = category_axis, value_axis
    if vtype == "combo" and any(s.get("secondary") for s in series):
        option["yAxis"] = [value_axis, {**_value_axis(ctx), "splitLine": {"show": False}}]
    highlight = None
    if results.get("highlight") and not leg:
        highlighted = frame(results["highlight"])
        if not highlighted.empty:
            highlight = highlighted.set_index("g0").reindex(raw_categories)[measures[0]["id"]].tolist()
    out_series = []
    for item in series:
        kind = (
            "bar"
            if vtype in {"bar", "column"} or (vtype == "combo" and not item.get("secondary"))
            else "line"
        )
        entry: dict[str, Any] = {
            "type": kind,
            "name": item["name"],
            "data": [
                None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 6)
                for v in item["values"]
            ],
            "itemStyle": {"color": item["color"]},
            "emphasis": {"focus": "series"},
        }
        if kind == "bar":
            entry.update(
                {
                    "barMaxWidth": 48,
                    "barCategoryGap": "35%",
                    "itemStyle": {
                        "color": item["color"],
                        "borderRadius": [0, 3, 3, 0] if horizontal else [3, 3, 0, 0],
                    },
                }
            )
            if stack != "none" and len(series) > 1:
                entry["stack"] = "total"
                entry["itemStyle"]["borderRadius"] = 0
            if highlight is not None:
                entry["itemStyle"]["opacity"] = 0.3
        else:
            entry.update(
                {
                    "smooth": bool(_fmt(visual, "line").get("smooth", False)),
                    "symbol": "circle",
                    "symbolSize": 5,
                    "showSymbol": len(labels) <= 24,
                    "lineStyle": {"width": 2, "color": item["color"]},
                }
            )
            if vtype == "area":
                entry["areaStyle"] = {"opacity": 0.15}
                if stack != "none" and len(series) > 1:
                    entry["stack"] = "total"
                    entry["areaStyle"] = {"opacity": 0.55}
            if item.get("secondary"):
                entry["yAxisIndex"] = 1
        if show_labels:
            entry["label"] = {
                "show": True,
                "position": ("right" if horizontal else "top")
                if stack == "none" or len(series) == 1
                else "inside",
                "color": ctx.s["text"] if stack == "none" or len(series) == 1 else "#FFFFFF",
                "fontSize": 11,
                "formatter": fn("percent", scale=100, digits=1) if percent else fn("labelValue"),
            }
        out_series.append(entry)
    if highlight is not None:
        out_series.append(
            {
                "type": "bar" if vtype in {"bar", "column", "combo"} else "line",
                "name": f"{series[0]['name']} (selected)",
                "data": [
                    None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 6)
                    for v in highlight
                ],
                "barGap": "-100%",
                "barMaxWidth": 48,
                "itemStyle": {
                    "color": series[0]["color"],
                    "borderRadius": [0, 3, 3, 0] if horizontal else [3, 3, 0, 0],
                },
                "lineStyle": {"width": 3, "color": series[0]["color"]},
                "z": 3,
                "silent": True,
            }
        )
    option["series"] = out_series
    if percent and stack == "percent":
        value_axis["max"] = 1
    columns = [nice(category_field)] + [s["name"] for s in series]
    rows = [[labels[i], *[s["values"][i] for s in series]] for i in range(len(labels))]
    default = (
        f"{series[0]['name'] if not leg else measure_label(measures[0])} by {nice(category_field).lower()}"
    )
    return {
        "option": option,
        "data": _data(columns, rows),
        "selectors": {
            "category": _selectors(category_field, raw_categories, grain),
            "series": series_selectors,
        },
        "notes": notes,
        "autoTitle": default,
    }


def build_pie(visual, queries, results, ctx):
    """donut, pie, funnel and treemap."""
    vtype = visual["type"]
    query = queries["main"]
    table, cat, _, measures, notes = _shape(visual, query, results["main"], ctx, top=False)
    if table.empty or not cat:
        return _empty(visual)
    field = query["groupBy"][0]["field"]
    grain = query["groupBy"][0].get("grain")
    value = measures[0]["id"]
    limit = (
        int(_fmt(visual, "donut").get("maxSlices", MAX_SLICES))
        if vtype in {"donut", "pie"}
        else 12
        if vtype == "funnel"
        else 30
    )
    additive = measures[0]["agg"] in {"sum", "count"}
    table, cut = top_n(table, cat, value, limit, True, additive)
    if cut:
        notes.append(f"Top {limit}" + (" + Others" if additive else ""))
    table = table.sort_values(value, ascending=False, kind="stable")
    raw = table[cat].tolist()
    labels = _labels(raw, grain)
    values = [
        None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
        for v in table[value].tolist()
    ]
    items = [
        {"name": label, "value": v, "itemStyle": {"color": ctx.category_color(field, r)}}
        for label, v, r in zip(labels, values, raw, strict=True)
    ]
    option = _base(ctx, legend=False, axis=False)
    option["tooltip"]["valueFormatter"] = fn("number")
    if vtype in {"donut", "pie"}:
        total = sum(v for v in values if v)
        series = {
            "type": "pie",
            "radius": ["58%", "80%"] if vtype == "donut" else [0, "78%"],
            "center": ["50%", "52%"],
            "avoidLabelOverlap": True,
            "itemStyle": {
                "borderColor": ctx.s["tile"],
                "borderWidth": 2,
                "borderRadius": 4 if vtype == "donut" else 0,
            },
            "label": {"show": True, "formatter": "{b} · {d}%", "color": ctx.s["mutedText"], "fontSize": 11},
            "labelLine": {"length": 8, "length2": 6},
            "data": items,
        }
        option["series"] = [series]
        if vtype == "donut":
            option["graphic"] = [
                {
                    "type": "text",
                    "left": "center",
                    "top": "45%",
                    "style": {
                        "text": compact(total),
                        "fill": ctx.s["text"],
                        "fontSize": 15,
                        "fontWeight": 700,
                        "align": "center",
                    },
                },
                {
                    "type": "text",
                    "left": "center",
                    "top": "56%",
                    "style": {"text": "Total", "fill": ctx.s["mutedText"], "fontSize": 11, "align": "center"},
                },
            ]
    elif vtype == "funnel":
        option["series"] = [
            {
                "type": "funnel",
                "left": "8%",
                "right": "8%",
                "top": 8,
                "bottom": 8,
                "sort": "descending",
                "gap": 3,
                "label": {
                    "show": True,
                    "position": "inside",
                    "formatter": "{b}",
                    "color": "#FFFFFF",
                    "fontSize": 11,
                },
                "data": items,
            }
        ]
    else:
        option["series"] = [
            {
                "type": "treemap",
                "roam": False,
                "nodeClick": False,
                "breadcrumb": {"show": False},
                "top": 0,
                "left": 0,
                "right": 0,
                "bottom": 0,
                "label": {"show": True, "formatter": "{b}", "fontSize": 12, "color": "#FFFFFF"},
                "upperLabel": {"show": False},
                "itemStyle": {"borderColor": ctx.s["tile"], "borderWidth": 2, "gapWidth": 2},
                "data": items,
            }
        ]
    return {
        "option": option,
        "data": _data(
            [nice(field), measure_label(measures[0])],
            [[label, v] for label, v in zip(labels, values, strict=True)],
        ),
        "selectors": {"category": _selectors(field, raw, grain)},
        "notes": notes,
        "autoTitle": f"{measure_label(measures[0])} by {nice(field).lower()}",
    }


def build_waterfall(visual, queries, results, ctx):
    query = queries["main"]
    table, cat, _, measures, notes = _shape(
        visual, query, results["main"], ctx, sort_by_value=False, top=False
    )
    if table.empty or not cat:
        return _empty(visual)
    grain = query["groupBy"][0].get("grain")
    field = query["groupBy"][0]["field"]
    raw = table[cat].tolist()[:40]
    labels = _labels(raw, grain) + ["Total"]
    values = [float(v or 0) for v in table[measures[0]["id"]].tolist()[:40]]
    base_values, ups, downs, running = [], [], [], 0.0
    for value in values:
        start = running
        running += value
        base_values.append(min(start, running))
        ups.append(value if value >= 0 else None)
        downs.append(-value if value < 0 else None)
    base_values.append(0)
    ups.append(None)
    downs.append(None)
    option = _base(ctx, legend=False)
    option["xAxis"] = _category_axis(ctx, labels, rotate=len(labels) > 8)
    option["yAxis"] = _value_axis(ctx)
    good, bad = ctx.theme.get("good", "#16A34A"), ctx.theme.get("bad", "#DC2626")
    option["series"] = [
        {
            "type": "bar",
            "stack": "w",
            "data": base_values,
            "itemStyle": {"color": "transparent"},
            "silent": True,
            "tooltip": {"show": False},
        },
        {
            "type": "bar",
            "stack": "w",
            "name": "Increase",
            "data": ups,
            "barMaxWidth": 48,
            "itemStyle": {"color": good, "borderRadius": [3, 3, 0, 0]},
        },
        {
            "type": "bar",
            "stack": "w",
            "name": "Decrease",
            "data": downs,
            "barMaxWidth": 48,
            "itemStyle": {"color": bad, "borderRadius": [3, 3, 0, 0]},
        },
        {
            "type": "bar",
            "stack": "w",
            "name": "Total",
            "data": [None] * (len(labels) - 1) + [running],
            "barMaxWidth": 48,
            "itemStyle": {"color": ctx.color(0), "borderRadius": [3, 3, 0, 0]},
        },
    ]
    return {
        "option": option,
        "data": _data(
            [nice(field), measure_label(measures[0])],
            [[label, v] for label, v in zip(labels, [*values, running], strict=True)],
        ),
        "selectors": {"category": [*_selectors(field, raw, grain), None]},
        "notes": notes,
        "autoTitle": f"{measure_label(measures[0])} change by {nice(field).lower()}",
    }


def build_scatter(visual, queries, results, ctx):
    query = queries["main"]
    table = frame(results["main"])
    if table.empty:
        return _empty(visual)
    measures = query["measures"]
    groups = query["groupBy"]
    field = groups[0]["field"] if groups else None
    x, y = measures[0]["id"], measures[1]["id"]
    size = measures[2]["id"] if len(measures) > 2 else None
    table = table.dropna(subset=[x, y]).head(2000)
    sizes = table[size].astype(float) if size else None
    biggest = float(sizes.max()) if sizes is not None and len(sizes) and sizes.max() else 1.0
    points = []
    for _, row in table.iterrows():
        name = str(row["g0"]) if field else ""
        point = {"name": name, "value": [float(row[x]), float(row[y])] + ([float(row[size])] if size else [])}
        point["itemStyle"] = {
            "color": ctx.category_color(field, row["g0"]) if field else ctx.color(0),
            "opacity": 0.8,
        }
        if size:
            point["symbolSize"] = 8 + 32 * math.sqrt(max(float(row[size]), 0) / biggest)
        points.append(point)
    option = _base(ctx, legend=False)
    option["tooltip"] = {**option["tooltip"], "trigger": "item", "formatter": fn("shapeTooltip")}
    option["xAxis"] = {
        **_value_axis(ctx, measure_label(measures[0])),
        "nameLocation": "middle",
        "nameGap": 26,
        "scale": True,
    }
    option["yAxis"] = {**_value_axis(ctx, measure_label(measures[1])), "scale": True}
    option["grid"]["bottom"] = 24
    option["series"] = [{"type": "scatter", "symbolSize": 10, "data": points, "emphasis": {"focus": "self"}}]
    columns = ([nice(field)] if field else []) + [measure_label(m) for m in measures]
    rows = [[p["name"], *p["value"]] if field else p["value"] for p in points]
    return {
        "option": option,
        "data": _data(columns, rows),
        "selectors": {"category": _selectors(field, table["g0"].tolist(), None) if field else []},
        "autoTitle": f"{measure_label(measures[1])} vs {measure_label(measures[0]).lower()}",
    }


def build_map(visual, queries, results, ctx, world_names, continents, geo_base):
    query = queries["main"]
    table = frame(results["main"])
    if table.empty:
        return _empty(visual)
    field = query["groupBy"][0]["field"]
    value = query["measures"][0]["id"]
    lookup = world_names()
    table["_country"] = table["g0"].astype(str).str.strip().str.lower().map(lookup)
    matched = table.dropna(subset=["_country"])
    notes = []
    missing = table[table["_country"].isna()]["g0"].astype(str).tolist()
    if missing:
        notes.append(f"{len(missing)} not matched: {', '.join(missing[:3])}")
    if matched.empty:
        return _empty(visual, "No values match country names")
    option = _base(ctx, legend=False, axis=False)
    seq = [ctx.s["gridline"], ctx.color(0)]
    option["visualMap"] = {
        "min": float(matched[value].min()),
        "max": float(matched[value].max()),
        "calculable": False,
        "orient": "horizontal",
        "left": "center",
        "bottom": 0,
        "itemHeight": 80,
        "itemWidth": 8,
        "text": ["High", "Low"],
        "textStyle": {"color": ctx.s["mutedText"], "fontSize": 10},
        "inRange": {"color": seq},
    }
    places = continents()
    african = [places.get(name) == "Africa" for name in matched["_country"]]
    scope = _fmt(visual, "map").get(
        "scope", "africa" if african and sum(african) / len(african) >= 0.6 else "world"
    )
    option["series"] = [
        {
            "type": "map",
            "name": measure_label(query["measures"][0]),
            **geo_base(scope),
            "itemStyle": {"areaColor": ctx.s["gridline"], "borderColor": ctx.s["tile"], "borderWidth": 0.6},
            "emphasis": {"label": {"show": False}, "itemStyle": {"areaColor": ctx.s["accent"]}},
            "data": [
                {"name": n, "value": float(v)}
                for n, v in zip(matched["_country"], matched[value], strict=True)
            ],
        }
    ]
    return {
        "option": option,
        "map": "world",
        "data": _data(
            [nice(field), measure_label(query["measures"][0])],
            [[g, v] for g, v in zip(table["g0"], table[value], strict=True)],
        ),
        "selectors": {
            "category": _selectors(field, matched["g0"].tolist(), None),
            "names": matched["_country"].tolist(),
        },
        "notes": notes,
        "autoTitle": f"{measure_label(query['measures'][0])} by {nice(field).lower()}",
    }


def build_table(visual, queries, results, ctx):
    query = queries["main"]
    table = frame(results["main"])
    if table.empty:
        return _empty(visual)
    groups, measures = query["groupBy"], query["measures"]
    for measure in measures:
        if measure.get("showValueAs"):
            table[measure["id"]] = show_value_as(table, measure["id"], measure["showValueAs"])
    sort = _fmt(visual, "sort")
    if measures and sort.get("by") != "category":
        table = table.sort_values(
            measures[0]["id"], ascending=sort.get("dir") == "asc", kind="stable", na_position="last"
        )
    columns = [{"key": f"g{i}", "label": nice(g["field"]), "align": "left"} for i, g in enumerate(groups)]
    columns += [
        {
            "key": m["id"],
            "label": measure_label(m),
            "align": "right",
            "percent": bool(m.get("showValueAs", "").startswith("pct")),
        }
        for m in measures
    ]
    rows = []
    for _, record in table.head(500).iterrows():
        row = {}
        for i, g in enumerate(groups):
            row[f"g{i}"] = period_label(record[f"g{i}"], g.get("grain"))
        for m in measures:
            value = record[m["id"]]
            row[m["id"]] = None if pd.isna(value) else float(value)
        rows.append(row)
    totals = {
        m["id"]: (
            float(table[m["id"]].sum()) if m["agg"] in {"sum", "count"} and not m.get("showValueAs") else None
        )
        for m in measures
    }
    bars = {}
    if _fmt(visual, "table").get("dataBars", True):
        for m in measures:
            peak = table[m["id"]].abs().max()
            bars[m["id"]] = float(peak) if peak and not pd.isna(peak) else None
    return {
        "table": {
            "columns": columns,
            "rows": rows,
            "totals": totals if groups else None,
            "bars": bars,
            "truncated": len(table.index) > 500,
        },
        "data": _data([c["label"] for c in columns], [[r[c["key"]] for c in columns] for r in rows]),
        "selectors": {
            "category": _selectors(groups[0]["field"], table["g0"].head(500).tolist(), groups[0].get("grain"))
            if groups
            else []
        },
        "autoTitle": " · ".join(c["label"] for c in columns[:3]),
    }


def build_matrix(visual, queries, results, ctx):
    query = queries["main"]
    table = frame(results["main"])
    if table.empty:
        return _empty(visual)
    groups, measures = query["groupBy"], query["measures"]
    value = measures[0]["id"]
    row_key = "g0"
    col_key = f"g{len(groups) - 1}" if len(groups) > 1 else None
    if col_key:
        pivot = table.pivot_table(index=row_key, columns=col_key, values=value, aggfunc="sum", sort=True)
    else:
        pivot = table.set_index(row_key)[[value]]
    pivot = pivot.head(200)
    col_grain = groups[-1].get("grain") if col_key else None
    columns = [str(c) for c in pivot.columns]
    col_labels = [
        period_label(c, col_grain) if col_key else measure_label(measures[0]) for c in pivot.columns
    ]
    finite = pivot.stack().dropna()
    low, high = (float(finite.min()), float(finite.max())) if len(finite) else (0.0, 1.0)
    accent = ctx.color(0)
    rows = []
    for index, values in pivot.iterrows():
        cells = []
        for column in pivot.columns:
            cell = values[column]
            cell = None if pd.isna(cell) else float(cell)
            share = 0 if cell is None or high == low else (cell - low) / (high - low)
            cells.append(
                {
                    "value": cell,
                    "display": compact(cell),
                    "shade": round(0.06 + 0.46 * share, 3) if cell is not None else 0,
                }
            )
        total = sum(c["value"] for c in cells if c["value"] is not None)
        rows.append(
            {
                "label": period_label(index, groups[0].get("grain")),
                "raw": index,
                "cells": cells,
                "total": total,
                "totalDisplay": compact(total),
            }
        )
    column_totals = [compact(float(pivot[c].sum())) for c in pivot.columns]
    return {
        "table": {
            "matrix": True,
            "rowHeader": nice(groups[0]["field"]),
            "columns": col_labels,
            "rows": rows,
            "columnTotals": column_totals,
            "grandTotal": compact(float(finite.sum())) if len(finite) else "—",
            "accent": accent,
        },
        "data": _data(
            [nice(groups[0]["field"]), *col_labels],
            [[r["label"], *[c["value"] for c in r["cells"]]] for r in rows],
        ),
        "selectors": {
            "category": _selectors(groups[0]["field"], list(pivot.index), groups[0].get("grain")),
            "columns": _selectors(groups[-1]["field"], columns, col_grain) if col_key else [],
        },
        "autoTitle": f"{measure_label(measures[0])} by {nice(groups[0]['field']).lower()}"
        + (f" and {nice(groups[-1]['field']).lower()}" if col_key else ""),
    }


def build_slicer(visual, queries, results, ctx):
    query = queries["main"]
    result = results["main"]
    if not query["groupBy"]:
        row = (result.get("rows") or [[None, None]])[0]
        field = query["measures"][0]["field"]
        return {
            "slicer": {"field": field, "label": nice(field), "range": {"min": row[0], "max": row[1]}},
            "autoTitle": nice(field),
        }
    field = query["groupBy"][0]["field"]
    options = [
        {"value": row[0], "label": BLANK if row[0] is None else str(row[0]), "count": row[1]}
        for row in result.get("rows") or []
    ]
    order = [str(v) for v in ctx.order.get(field, [])]
    options.sort(key=lambda o: (order.index(o["label"]) if o["label"] in order else len(order), o["label"]))
    return {
        "autoTitle": nice(field),
        "slicer": {
            "field": field,
            "label": nice(field),
            "options": options[:1000],
            "truncated": result.get("truncated", False),
        },
    }


HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
LEGEND_POSITIONS = {
    "top": ({"top": 0, "right": 0}, None),
    "bottom": ({"bottom": 0, "left": "center"}, ("bottom", 28)),
    "left": ({"left": 0, "top": "middle", "orient": "vertical"}, ("left", 96)),
    "right": ({"right": 0, "top": "middle", "orient": "vertical"}, ("right", 96)),
}


def build_chart(visual, queries, results, ctx, render, kinds):
    """A gallery chart (any of the catalog's charts) drawn by its Python renderer on filtered rows."""
    result = results["main"]
    columns = list(result.get("columns") or [])
    table = pd.DataFrame(result.get("rows") or [], columns=columns)
    if table.empty:
        return _empty(visual)
    for column in columns:
        if kinds.get(column) == "datetime":
            table[column] = pd.to_datetime(table[column], errors="coerce")
        elif kinds.get(column) == "numeric":
            table[column] = pd.to_numeric(table[column], errors="coerce")
    chart = _fmt(visual, "chart")
    style = {
        "colors": ctx.colors[:12],
        "theme": ctx.theme.get("mode", "light"),
        "title": _fmt(visual, "title").get("text") or "",
        "showLegend": _fmt(visual, "legend").get("show", True) is not False,
    }
    rendered = render(
        table,
        {column: kinds.get(column, "categorical") for column in columns},
        {
            "chart_type": chart.get("chartType"),
            "fields": chart.get("fields") or {},
            "options": chart.get("options") or {},
            "style": style,
        },
    )
    option = rendered["option"]
    option["backgroundColor"] = "transparent"
    notes = list(rendered.get("notes") or [])
    if result.get("truncated"):
        notes.append("Sampled")
    return {
        "option": option,
        "map": rendered.get("map"),
        "data": rendered.get("table"),
        "notes": notes,
        "autoTitle": rendered.get("title"),
    }


def _apply_legend(visual: dict[str, Any], option: dict[str, Any]) -> None:
    legend = _fmt(visual, "legend")
    if not isinstance(option.get("legend"), dict) or not legend:
        return
    if legend.get("show") is False:
        option["legend"] = {"show": False}
        return
    placement = LEGEND_POSITIONS.get(legend.get("position", ""))
    if not placement:
        return
    for key in ("top", "bottom", "left", "right", "orient"):
        option["legend"].pop(key, None)
    option["legend"].update(placement[0])
    grid = option.get("grid")
    if isinstance(grid, dict):
        grid["top"] = 12
        if placement[1]:
            side, room = placement[1]
            grid[side] = max(int(grid.get(side, 0) or 0), room)


def build(
    visual: dict[str, Any], queries: dict[str, Any], results: dict[str, Any], ctx: Ctx, **geo: Any
) -> dict[str, Any]:
    vtype = visual.get("type")
    custom = [c for c in _fmt(visual, "colors").get("series") or [] if isinstance(c, str) and HEX.match(c)][
        :12
    ]
    if custom:
        ctx = ctx.with_colors(custom)
    if vtype == "text":
        out: dict[str, Any] = {}
    elif vtype in {"card", "multi_card"}:
        out = build_cards(visual, queries, results, ctx)
    elif vtype == "kpi":
        out = build_kpi(visual, queries, results, ctx)
    elif vtype == "gauge":
        out = build_gauge(visual, queries, results, ctx)
    elif vtype in {"bar", "column", "line", "area", "combo"}:
        out = build_category(visual, queries, results, ctx)
    elif vtype in {"donut", "pie", "funnel", "treemap"}:
        out = build_pie(visual, queries, results, ctx)
    elif vtype == "waterfall":
        out = build_waterfall(visual, queries, results, ctx)
    elif vtype == "scatter":
        out = build_scatter(visual, queries, results, ctx)
    elif vtype == "map":
        out = build_map(visual, queries, results, ctx, geo["world_names"], geo["continents"], geo["geo_base"])
    elif vtype == "table":
        out = build_table(visual, queries, results, ctx)
    elif vtype == "matrix":
        out = build_matrix(visual, queries, results, ctx)
    elif vtype == "slicer":
        out = build_slicer(visual, queries, results, ctx)
    elif vtype == "chart":
        out = build_chart(visual, queries, results, ctx, geo["render"], geo.get("kinds") or {})
    else:
        return {
            "id": visual.get("id"),
            "type": vtype,
            "status": "error",
            "message": "Unsupported visual type",
        }
    if out.get("status") == "empty":
        return out
    if isinstance(out.get("option"), dict):
        _apply_legend(visual, out["option"])
    auto = out.pop("autoTitle", None)
    title = _title(visual, auto or (out.get("kpi") or {}).get("label") or "")
    truncated = any(r.get("truncated") for r in results.values())
    return {
        "id": visual.get("id"),
        "type": vtype,
        "status": "ok",
        "title": title,
        "truncated": truncated,
        **out,
    }
