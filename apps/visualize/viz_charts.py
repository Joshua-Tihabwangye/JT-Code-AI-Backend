"""Apache ECharts option builders for every catalog chart.

Python (pandas, NumPy, SciPy, statsmodels, scikit-learn) does all the data work;
each renderer returns a plain-JSON ECharts ``option``. JSON cannot carry
functions, so the few places that need one use hydration markers,
``{"$fn": "<name>", ...}``, which the web app and exported reports replace with a
small fixed set of client functions (formatters and the generic ``shapes``
renderer for custom series). No request ever carries code.
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if "jt_viz_data" not in sys.modules:  # loaded by path inside the sandbox
    import importlib.util

    _spec = importlib.util.spec_from_file_location("jt_viz_data", _HERE / "viz_data.py")
    assert _spec and _spec.loader
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["jt_viz_data"] = _module
    _spec.loader.exec_module(_module)
d = sys.modules["jt_viz_data"]
catalog = d.catalog
AnalysisError = d.AnalysisError
MAX_CATEGORIES = d.MAX_CATEGORIES
GRAIN_FREQ = d.GRAIN_FREQ
SEASON = d.SEASON

FONT = "Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
POSITIVE = "#10B981"
NEGATIVE = "#EF4444"
THEME_COLORS = {
    "light": {"bg": "#FFFFFF", "fg": "#0F172A", "muted": "#64748B", "grid": "#E2E8F0", "line": "#CBD5E1"},
    "dark": {"bg": "#0B1220", "fg": "#E2E8F0", "muted": "#94A3B8", "grid": "#1E293B", "line": "#334155"},
}
DIVERGING = ["#2166AC", "#67A9CF", "#D1E5F0", "#F7F7F7", "#FDDBC7", "#EF8A62", "#B2182B"]
WORLD_PATH = _HERE / "static" / "world.json"
SCOPES = {
    "world": ([10, 20], 1.1),
    "africa": ([20, 2], 2.6),
    "europe": ([15, 53], 4.2),
    "asia": ([95, 32], 2.2),
    "north america": ([-100, 45], 2.4),
    "south america": ([-60, -20], 2.6),
}


# --- Option helpers ----------------------------------------------------------------------------


def fn(name: str, **arguments: Any) -> dict[str, Any]:
    """A hydration marker the client replaces with a fixed, named function."""
    return {"$fn": name, **arguments}


def num(value: Any, digits: int = 6) -> float | None:
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return round(number, digits)


def nums(values: Any, digits: int = 6) -> list[float | None]:
    return [num(value, digits) for value in list(values)]


def labels(values: Any) -> list[str]:
    return [str(value) for value in list(values)]


def dates(series: Any) -> list[str]:
    import pandas as pd

    stamps = pd.to_datetime(series)
    has_time = bool(((stamps.dt.hour != 0) | (stamps.dt.minute != 0)).any()) if len(stamps) else False
    return list(stamps.dt.strftime("%Y-%m-%d %H:%M" if has_time else "%Y-%m-%d"))


def rgba(hex_color: str, alpha: float) -> str:
    value = hex_color.lstrip("#")
    red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
    return f"rgba({red},{green},{blue},{alpha})"


def compact(value: float) -> str:
    magnitude = abs(value)
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if magnitude >= size:
            return f"{value / size:.1f}{suffix}"
    return f"{value:.3g}" if magnitude < 100 else f"{value:,.0f}"


def value_axis(name: str = "", *, log: bool = False, scale: bool = False, **extra: Any) -> dict[str, Any]:
    axis: dict[str, Any] = {
        "type": "log" if log else "value",
        "name": name,
        "nameLocation": "middle",
        "nameGap": 42,
        "scale": scale,
        "axisLabel": {"formatter": fn("compact")},
    }
    axis.update(extra)
    return axis


def category_axis(values: Any, name: str = "", **extra: Any) -> dict[str, Any]:
    axis: dict[str, Any] = {
        "type": "category",
        "data": labels(values),
        "name": name,
        "nameLocation": "middle",
        "nameGap": 32,
        "axisLabel": {"hideOverlap": True},
    }
    axis.update(extra)
    return axis


def time_axis(name: str = "", **extra: Any) -> dict[str, Any]:
    return {"type": "time", "name": name, "nameLocation": "middle", "nameGap": 32, **extra}


def index_axis(names: list[str], name: str = "", **extra: Any) -> dict[str, Any]:
    """A value axis whose integer ticks show group names (for jittered and custom charts)."""
    return {
        "type": "value",
        "name": name,
        "nameLocation": "middle",
        "nameGap": 36,
        "min": -0.6,
        "max": len(names) - 0.4,
        "interval": 1,
        "splitLine": {"show": False},
        "axisLabel": {"formatter": fn("indexLabel", labels=names)},
        **extra,
    }


def base(*, trigger: str = "axis", legend: bool = True, zoom: bool = False) -> dict[str, Any]:
    option: dict[str, Any] = {
        "tooltip": {"trigger": trigger, "confine": True, "valueFormatter": fn("number")},
        "legend": {"type": "scroll", "top": 0, "left": 0, "right": 90} if legend else {"show": False},
        "grid": {
            "left": 16,
            "right": 28,
            "top": 44 if legend else 28,
            "bottom": 40 if zoom else 16,
            "containLabel": True,
        },
        "series": [],
    }
    if zoom:
        option["dataZoom"] = [{"type": "inside"}, {"type": "slider", "height": 18, "bottom": 6}]
    return option


def shapes_series(items: list[dict[str, Any]], *, name: str = "", **extra: Any) -> dict[str, Any]:
    """A custom series drawn by the client ``shapes`` renderer (polygons, polylines, text)."""
    return {
        "type": "custom",
        "name": name,
        "renderItem": fn("shapes"),
        "data": items,
        "encode": {"x": 0, "y": 1},
        **extra,
    }


def panels(count: int, columns: int, *, top: float = 8, gap: float = 9) -> list[dict[str, str]]:
    """Percent-positioned grids for small multiples."""
    columns = max(1, min(columns, count))
    rows = math.ceil(count / columns)
    width = (100 - 4 - gap * (columns - 1)) / columns
    height = (100 - top - 6 - gap * (rows - 1)) / rows
    grids = []
    for index in range(count):
        row, column = divmod(index, columns)
        grids.append(
            {
                "left": f"{3 + column * (width + gap):.2f}%",
                "top": f"{top + row * (height + gap):.2f}%",
                "width": f"{width:.2f}%",
                "height": f"{height:.2f}%",
                "containLabel": False,
            }
        )
    return grids


def plain(value: Any) -> Any:
    """Convert NumPy/pandas scalars to JSON-safe Python values (NaN/inf become null)."""
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except ValueError, TypeError:
            return str(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _pct(value: str) -> float:
    return float(value.rstrip("%"))


def _deep_default(target: dict[str, Any], defaults: dict[str, Any]) -> None:
    for key, value in defaults.items():
        if isinstance(value, dict):
            current = target.setdefault(key, {})
            if isinstance(current, dict):
                _deep_default(current, value)
        else:
            target.setdefault(key, value)


def _each(option: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = option.get(key)
    if isinstance(value, dict):
        return [value]
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


nice = catalog.nice
humanize_text = catalog.humanize_text
_code_like = catalog.code_like


def label_map(columns: Any) -> dict[str, str]:
    """Column name → readable label (every column; unchanged names map to themselves)."""
    return {str(column): nice(column) for column in columns}


def humanize_option(option: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    """Readable axis, series, legend and indicator names; data values are left as they are."""
    if not mapping:
        return option

    def label(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return mapping.get(value) or humanize_text(value, mapping)

    for key in (
        "xAxis",
        "yAxis",
        "radiusAxis",
        "angleAxis",
        "singleAxis",
        "parallelAxis",
        "xAxis3D",
        "yAxis3D",
        "zAxis3D",
    ):
        for axis in _each(option, key):
            if "name" in axis:
                axis["name"] = label(axis["name"])
            data = axis.get("data")
            # Axes that list columns (correlation matrices, feature charts) show readable names too.
            if (
                isinstance(data, list)
                and data
                and all(isinstance(item, str) and item in mapping for item in data)
            ):
                axis["data"] = [mapping[item] for item in data]
    for series in option.get("series") or []:
        if isinstance(series, dict) and "name" in series:
            series["name"] = label(series["name"])
    for legend in _each(option, "legend"):
        if isinstance(legend.get("data"), list):
            legend["data"] = [
                {**item, "name": label(item.get("name"))} if isinstance(item, dict) else label(item)
                for item in legend["data"]
            ]
        if isinstance(legend.get("selected"), dict):
            legend["selected"] = {label(key): value for key, value in legend["selected"].items()}
    for radar in _each(option, "radar"):
        for indicator in radar.get("indicator") or []:
            if isinstance(indicator, dict) and "name" in indicator:
                indicator["name"] = label(indicator["name"])
    for title in _each(option, "title"):
        for key in ("text", "subtext"):
            if key in title:
                title[key] = humanize_text(title[key], mapping)
    return option


HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
TABLE_ROWS = 500


def _cell(value: Any) -> Any:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, (list, tuple)):
        return None
    if isinstance(value, float):
        return num(value, 4)
    return value


def _first(option: dict[str, Any], key: str) -> dict[str, Any]:
    items = _each(option, key)
    return items[0] if items else {}


def _visible_series(option: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for series in option.get("series") or []:
        if series.get("silent") and (series.get("tooltip") or {}).get("show") is False:
            continue
        if series.get("type") == "bar" and (series.get("itemStyle") or {}).get("color") == "transparent":
            continue
        out.append(series)
    return out


def _flatten(nodes: list[dict[str, Any]], prefix: str = "") -> list[list[Any]]:
    rows: list[list[Any]] = []
    for node in nodes or []:
        name = str(node.get("name", "")).split("\n")[0]
        label = f"{prefix} › {name}" if prefix else name
        rows.append([label, _cell(node.get("value"))])
        rows += _flatten(node.get("children") or [], label)
    return rows


def _category_rows(
    series: list[dict[str, Any]], category: dict[str, Any], horizontal: bool
) -> tuple[list[str], list[list[Any]]]:
    labels_ = [str(value) for value in category.get("data") or []]
    columns = [str(category.get("name") or "Category")]
    values: list[dict[str, Any]] = []
    for item in series:
        if item.get("type") in {"custom", "lines"}:
            continue
        mapping: dict[str, Any] = {}
        for index, point in enumerate(item.get("data") or []):
            raw = point.get("value") if isinstance(point, dict) else point
            if isinstance(raw, list) and len(raw) == 2:
                key, value = (raw[1], raw[0]) if horizontal else (raw[0], raw[1])
                mapping[str(key)] = value
            elif index < len(labels_):
                mapping[labels_[index]] = raw
        columns.append(str(item.get("name") or f"Series {len(columns)}"))
        values.append(mapping)
    return columns, [[label, *[_cell(mapping.get(label)) for mapping in values]] for label in labels_]


def _point_rows(series: list[dict[str, Any]], option: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    x_axis, y_axis = _first(option, "xAxis"), _first(option, "yAxis")
    x_label = x_axis.get("name") or ("Date" if x_axis.get("type") == "time" else "x")
    columns = ["Series", str(x_label), str(y_axis.get("name") or "y")]
    rows: list[list[Any]] = []
    for item in series:
        for point in item.get("data") or []:
            raw = point.get("value") if isinstance(point, dict) else point
            if isinstance(raw, list) and len(raw) >= 2:
                rows.append([item.get("name") or "", *[_cell(v) for v in raw[:3]]])
            if len(rows) > TABLE_ROWS:
                return columns + (["z"] if any(len(r) > 3 for r in rows) else []), rows
    return columns + (["z"] if any(len(r) > 3 for r in rows) else []), rows


def option_table(option: dict[str, Any]) -> dict[str, Any]:
    """The data a chart shows, organized as a table (for “View data” and exports)."""
    series = _visible_series(option)
    kinds = {str(item.get("type")) for item in series}
    x_axis, y_axis = _first(option, "xAxis"), _first(option, "yAxis")
    first = series[0] if series else {}
    columns: list[str] = []
    rows: list[list[Any]] = []
    if kinds & {"pie", "funnel", "map", "gauge"}:
        data = [item for item in first.get("data") or [] if isinstance(item, dict)]
        total = sum(float(item.get("value") or 0) for item in data) or 1
        columns = ["Name", str(first.get("name") or "Value"), "Share"]
        rows = [
            [item.get("name"), _cell(item.get("value")), f"{float(item.get('value') or 0) / total:.1%}"]
            for item in data
        ]
    elif kinds & {"treemap", "sunburst", "tree"}:
        columns, rows = ["Path", "Value"], _flatten(first.get("data") or [])
    elif "sankey" in kinds:
        columns = ["From", "To", "Value"]
        rows = [
            [
                str(link["source"]).strip(chr(0x200B)),
                str(link["target"]).strip(chr(0x200B)),
                _cell(link["value"]),
            ]
            for link in first.get("links") or []
        ]
    elif "graph" in kinds:
        columns = ["From", "To", "Weight"]
        rows = [
            [link.get("source"), link.get("target"), _cell(link.get("value"))]
            for link in first.get("links") or []
        ]
    elif "radar" in kinds:
        names = [str(item.get("name")) for item in (_first(option, "radar").get("indicator") or [])]
        columns = ["Item", *names]
        rows = [
            [item.get("name"), *[num(v, 4) for v in item.get("value") or []]]
            for item in first.get("data") or []
        ]
    elif "parallel" in kinds:
        columns = [str(axis.get("name")) for axis in _each(option, "parallelAxis")]
        rows = [list(row) for row in first.get("data") or []]
    elif "heatmap" in kinds and kinds & {"bar", "line"}:
        columns, rows = _category_rows(
            [item for item in series if item.get("type") != "heatmap"], x_axis, horizontal=False
        )
    elif "heatmap" in kinds and x_axis.get("type") == "category" and y_axis.get("type") == "category":
        xs, ys = x_axis.get("data") or [], y_axis.get("data") or []
        grid: dict[tuple[int, int], Any] = {}
        for cell in first.get("data") or []:
            if isinstance(cell, list) and len(cell) >= 3:
                grid[(int(cell[0]), int(cell[1]))] = cell[2]
        columns = [str(y_axis.get("name") or ""), *[str(x) for x in xs]]
        rows = [[str(y), *[grid.get((i, j)) for i in range(len(xs))]] for j, y in enumerate(ys)]
    elif "heatmap" in kinds:
        columns = ["Date", "Value"]
        rows = [list(cell)[:2] for cell in first.get("data") or [] if isinstance(cell, list)]
    elif "boxplot" in kinds:
        groups = x_axis.get("data") or y_axis.get("data") or []
        columns = ["Group", "Series", "Min", "Q1", "Median", "Q3", "Max"]
        for item in series:
            if item.get("type") == "boxplot":
                for group, box in zip(groups, item.get("data") or [], strict=False):
                    if box:
                        rows.append([group, item.get("name"), *box])
    elif "candlestick" in kinds:
        columns = ["Date", "Open", "Close", "Low", "High"]
        rows = [
            [date, *values]
            for date, values in zip(x_axis.get("data") or [], first.get("data") or [], strict=False)
        ]
    elif x_axis.get("type") == "category" and x_axis.get("data") is not None:
        columns, rows = _category_rows(series, x_axis, horizontal=False)
    elif y_axis.get("type") == "category" and y_axis.get("data") is not None:
        columns, rows = _category_rows(series, y_axis, horizontal=True)
    else:
        columns, rows = _point_rows(series, option)
    return {"columns": columns, "rows": rows[:TABLE_ROWS], "truncated": len(rows) > TABLE_ROWS}


def finish(option: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Apply the palette and theme to every component of an option."""
    tokens = ctx["tokens"]
    option.setdefault("color", ctx["colors"])
    option["backgroundColor"] = tokens["bg"]
    option["textStyle"] = {"fontFamily": FONT, "color": tokens["fg"]}
    option.setdefault("animationDuration", 500)
    axis_style = {
        "axisLine": {"lineStyle": {"color": tokens["line"]}},
        "axisTick": {"lineStyle": {"color": tokens["line"]}},
        "splitLine": {"lineStyle": {"color": tokens["grid"]}},
        "axisLabel": {"color": tokens["muted"]},
        "nameTextStyle": {"color": tokens["muted"]},
    }
    for key in ("xAxis", "yAxis", "radiusAxis", "angleAxis", "singleAxis", "parallelAxis"):
        for axis in _each(option, key):
            _deep_default(axis, axis_style)
    for key in ("xAxis3D", "yAxis3D", "zAxis3D"):
        for axis in _each(option, key):
            _deep_default(
                axis,
                {
                    "axisLine": {"lineStyle": {"color": tokens["muted"]}},
                    "axisLabel": {"textStyle": {"color": tokens["muted"]}},
                    "nameTextStyle": {"color": tokens["fg"]},
                    "splitLine": {"lineStyle": {"color": tokens["grid"]}},
                },
            )
    for legend in _each(option, "legend"):
        _deep_default(
            legend, {"textStyle": {"color": tokens["muted"]}, "pageTextStyle": {"color": tokens["muted"]}}
        )
    for visual in _each(option, "visualMap"):
        _deep_default(visual, {"textStyle": {"color": tokens["muted"]}})
    for radar in _each(option, "radar"):
        _deep_default(
            radar,
            {
                "axisName": {"color": tokens["fg"]},
                "splitLine": {"lineStyle": {"color": tokens["grid"]}},
                "axisLine": {"lineStyle": {"color": tokens["line"]}},
                "splitArea": {"show": False},
            },
        )
    for title in _each(option, "title"):
        _deep_default(
            title,
            {
                "textStyle": {"color": tokens["fg"], "fontSize": 13, "fontWeight": 600},
                "subtextStyle": {"color": tokens["muted"]},
            },
        )
    for tooltip in _each(option, "tooltip"):
        _deep_default(
            tooltip,
            {
                "backgroundColor": tokens["bg"],
                "borderColor": tokens["line"],
                "textStyle": {"color": tokens["fg"]},
            },
        )
    option["toolbox"] = {
        "right": 4,
        "top": 0,
        "itemSize": 13,
        "iconStyle": {"borderColor": tokens["muted"]},
        "feature": {
            "saveAsImage": {"pixelRatio": 2, "backgroundColor": tokens["bg"]},
            "dataView": {
                "readOnly": True,
                "title": "View data",
                "lang": ["Data shown in this chart", "Close", "Refresh"],
                "backgroundColor": tokens["bg"],
                "textColor": tokens["fg"],
                "textareaColor": tokens["bg"],
                "buttonColor": ctx["colors"][0],
                "optionToContent": fn("dataTable", table=plain(option_table(option))),
            },
            "restore": {},
        },
    }
    return dict(plain(option))


def _stat(ctx: dict[str, Any], label: str, value: Any, fmt: str = "number") -> None:
    d._stat(ctx, label, value, fmt)


def _insight(ctx: dict[str, Any], text: str) -> None:
    ctx.setdefault("insight", text)


def humanize_context(ctx: dict[str, Any], mapping: dict[str, str]) -> None:
    """Readable column names in the takeaway, notes and stat labels."""
    if not mapping:
        return
    if ctx.get("insight"):
        ctx["insight"] = humanize_text(ctx["insight"], mapping, capital=True)
    ctx["notes"] = [humanize_text(note, mapping) for note in ctx["notes"]]
    for stat in ctx["stats"]:
        stat["label"] = mapping.get(stat.get("label"), humanize_text(stat.get("label"), mapping))


def _limit_series(frame: Any, column: str | None, ctx: dict[str, Any], weight: str | None = None) -> Any:
    """Keep the largest groups of a colour column (the user's “max lines” setting); merge the rest."""
    if not column:
        return frame
    data = d._labels(frame, [column])
    limit = int(ctx.get("max_series") or 12)
    if data[column].nunique() <= limit:
        return data
    if weight:
        sizes = data.groupby(column)[weight].apply(lambda s: d._numeric(s.to_frame(), weight).abs().sum())
    else:
        sizes = data[column].value_counts()
    keep = set(sizes.sort_values(ascending=False).head(limit - 1).index)
    data[column] = data[column].where(data[column].isin(keep), "Other")
    ctx["notes"].append(f"Showing the {limit - 1} largest {column} groups; the rest are combined as “Other”.")
    return data


def _groups_in_order(data: Any, column: str, value: str | None = None) -> list[str]:
    if value and value in data.columns:
        totals = data.groupby(column)[value].sum().sort_values(ascending=False)
        return labels(totals.index)
    return labels(data[column].value_counts().index)


# --- Comparison --------------------------------------------------------------------------------


def r_bar(frame, f, o, ctx, mode: str = "stack", percent: bool = False):
    x, y, color, facet = f["x"], f.get("y"), f.get("color"), f.get("facet")
    frame = _limit_series(frame, color, ctx, y)
    by = [x] + [column for column in (color, facet) if column and column != x]
    data, value = d._aggregate(frame, by, y, o.get("aggregation", "sum"))
    order = d._order(data, x, value, o, ctx)
    data = data[data[x].astype(str).isin(order)]
    if percent:
        totals = data.groupby(x)[value].transform("sum")
        data[value] = (data[value] / totals.where(totals != 0) * 100).round(2)
    horizontal = o.get("orientation") == "h"
    groups = _groups_in_order(data, color, value) if color else [None]
    facets = labels(data[facet].unique())[:6] if facet else [None]
    option = base(trigger="axis")
    option["tooltip"]["axisPointer"] = {"type": "shadow"}
    grids = panels(len(facets), 3, top=12) if facet else [option["grid"]]
    option["grid"] = grids
    option["xAxis"], option["yAxis"], option["title"] = [], [], []
    value_name = "Share (%)" if percent else d._value_label(y, o)
    for index, panel in enumerate(facets):
        subset = data if panel is None else data[data[facet].astype(str) == panel]
        cat = category_axis(order, "" if horizontal else x, gridIndex=index)
        val = value_axis(
            "" if horizontal and facet else value_name, log=bool(o.get("log_y")), gridIndex=index
        )
        if percent:
            val.update({"max": 100, "axisLabel": {"formatter": "{value}%"}})
        if horizontal:
            cat["inverse"] = True
            option["xAxis"].append(val)
            option["yAxis"].append(cat)
        else:
            option["xAxis"].append(cat)
            option["yAxis"].append(val)
        if panel is not None:
            option["title"].append(
                {
                    "text": f"{facet}: {panel}",
                    "left": grids[index]["left"],
                    "top": f"{float(grids[index]['top'][:-1]) - 4:.1f}%",
                }
            )
        for group_index, group in enumerate(groups):
            part = subset if group is None else subset[subset[color].astype(str) == group]
            lookup = dict(zip(part[x].astype(str), part[value], strict=False))
            series = {
                "type": "bar",
                "name": group or value_name,
                "data": [num(lookup.get(category)) for category in order],
                "xAxisIndex": index,
                "yAxisIndex": index,
                "barMaxWidth": 56,
                "emphasis": {"focus": "series"},
                "itemStyle": {"borderRadius": [4, 4, 0, 0] if not horizontal else [0, 4, 4, 0]}
                if mode != "stack" or not color
                else {},
            }
            if color and mode == "stack":
                series["stack"] = f"total-{index}"
            if not color:
                series["itemStyle"]["color"] = ctx["colors"][0]
            if o.get("show_values"):
                series["label"] = {
                    "show": True,
                    "position": "inside" if color and mode == "stack" else ("right" if horizontal else "top"),
                    "formatter": fn("labelValue", percent=percent),
                }
            option["series"].append(series)
            del group_index
    option["legend"]["show"] = bool(color)
    if not option["title"]:
        option.pop("title")
    if not color and not facet and len(data):
        totals = data.groupby(x)[value].sum().sort_values(ascending=False)
        share = totals.iloc[0] / totals.sum() if totals.sum() else 0
        _insight(
            ctx,
            f"{totals.index[0]} is the largest {x} with {compact(float(totals.iloc[0]))}"
            + (f" ({share:.0%} of the total)." if 0 < share <= 1 and not percent else "."),
        )
    elif color and len(data):
        top = data.groupby(color)[value].sum().sort_values(ascending=False)
        _insight(
            ctx,
            f"Across all {x} values, {top.index[0]} contributes the most ({compact(float(top.iloc[0]))}).",
        )
    return option


def r_grouped_bar(frame, f, o, ctx):
    return r_bar(frame, f, o, ctx, mode="group")


def r_stacked_bar(frame, f, o, ctx):
    return r_bar(frame, f, o, ctx, mode="stack")


def r_percent_stacked_bar(frame, f, o, ctx):
    return r_bar(frame, f, o, ctx, mode="stack", percent=True)


def r_lollipop(frame, f, o, ctx):
    x, y = f["x"], f.get("y")
    data, value = d._aggregate(frame, [x], y, o.get("aggregation", "sum"))
    order = d._order(data, x, value, o, ctx)
    lookup = dict(zip(data[x].astype(str), data[value], strict=False))
    values = [num(lookup.get(category)) for category in order]
    option = base(legend=False)
    option["tooltip"]["axisPointer"] = {"type": "shadow"}
    option["xAxis"] = value_axis(d._value_label(y, o))
    option["yAxis"] = category_axis(order, inverse=True)
    option["series"] = [
        {
            "type": "bar",
            "data": values,
            "barWidth": 3,
            "itemStyle": {"color": ctx["tokens"]["line"]},
            "silent": True,
            "tooltip": {"show": False},
        },
        {
            "type": "scatter",
            "name": d._value_label(y, o),
            "data": values,
            "symbolSize": 16,
            "itemStyle": {"color": ctx["colors"][0], "borderColor": ctx["tokens"]["bg"], "borderWidth": 2},
        },
    ]
    if order:
        _insight(ctx, f"{order[0]} leads with {compact(values[0] or 0)}.")
    return option


def r_dot_plot(frame, f, o, ctx):
    x, ys = f["x"], f["y"]
    data = d._labels(frame, [x])
    for column in ys:
        data[column] = d._numeric(data, column)
    how = o.get("aggregation", "mean")
    grouped = data.groupby(x, sort=False)[ys].agg(how).reset_index()
    if len(grouped.index) > MAX_CATEGORIES:
        grouped = grouped.nlargest(MAX_CATEGORIES, ys[0])
        ctx["notes"].append(f"Showing the {MAX_CATEGORIES} largest categories.")
    option = base()
    option["tooltip"]["axisPointer"] = {"type": "line", "axis": "y"}
    option["xAxis"] = value_axis("Value")
    option["yAxis"] = category_axis(grouped[x])
    option["series"] = [
        {
            "type": "scatter",
            "name": column,
            "data": [[num(v), str(c)] for c, v in zip(grouped[x], grouped[column], strict=True)],
            "symbolSize": 14,
        }
        for column in ys
    ]
    return option


def r_dumbbell(frame, f, o, ctx):
    x, start, end = f["x"], f["start"], f["end"]
    data = d._labels(frame, [x])
    data[start], data[end] = d._numeric(data, start), d._numeric(data, end)
    grouped = data.groupby(x, sort=False)[[start, end]].agg(o.get("aggregation", "sum")).reset_index()
    grouped["_gap"] = grouped[end] - grouped[start]
    order = d._order(grouped.assign(_v=grouped["_gap"]), x, "_v", o, ctx)
    grouped = grouped.set_index(x).loc[order].reset_index()
    option = base()
    option["tooltip"]["trigger"] = "item"
    option["xAxis"] = value_axis("Value")
    option["yAxis"] = category_axis(order, inverse=True)
    segments = [
        {
            "value": [num(row[start]), index],
            "shapes": [
                {
                    "type": "polyline",
                    "points": [[num(row[start]), index], [num(row[end]), index]],
                    "color": rgba(POSITIVE if row[end] >= row[start] else NEGATIVE, 0.6),
                    "width": 5,
                }
            ],
        }
        for index, (_, row) in enumerate(grouped.iterrows())
    ]
    option["series"] = [
        shapes_series(segments, name="Gap", silent=True, tooltip={"show": False}, z=1),
        {
            "type": "scatter",
            "name": start,
            "data": [[num(v), c] for c, v in zip(grouped[x], grouped[start], strict=True)],
            "symbolSize": 15,
            "z": 3,
        },
        {
            "type": "scatter",
            "name": end,
            "data": [[num(v), c] for c, v in zip(grouped[x], grouped[end], strict=True)],
            "symbolSize": 15,
            "z": 3,
        },
    ]
    if len(grouped.index):
        widest = grouped.loc[grouped["_gap"].abs().idxmax()]
        _insight(
            ctx,
            (
                f"The biggest change is in {widest[x]}: {compact(float(widest[start]))} → "
                f"{compact(float(widest[end]))}."
            ),
        )
    return option


def r_radar(frame, f, o, ctx):
    x, features = f["x"], f["features"]
    data = d._labels(frame, [x])
    for column in features:
        data[column] = d._numeric(data, column)
    grouped = data.groupby(x, sort=False)[features].agg(o.get("aggregation", "mean"))
    if len(grouped.index) > 8:
        grouped = grouped.loc[grouped.sum(axis=1).nlargest(8).index]
        ctx["notes"].append("Showing the 8 largest items; radar charts become unreadable with more.")
    scaled = grouped
    spans = grouped.max() - grouped.min()
    if len(features) > 1 and grouped.abs().max().max() / max(grouped.abs().max().min(), 1e-9) > 10:
        scaled = (grouped - grouped.min()) / spans.where(spans != 0, 1)
        ctx["notes"].append("Measures have different scales, so each is scaled to 0–1.")
    option = base(trigger="item")
    option["radar"] = {
        "indicator": [
            {
                "name": column,
                "max": num(scaled[column].max() * 1.1) or 1,
                "min": min(0, num(scaled[column].min()) or 0),
            }
            for column in features
        ],
        "radius": "68%",
        "center": ["50%", "56%"],
    }
    option["series"] = [
        {
            "type": "radar",
            "data": [
                {"name": str(label), "value": nums(row.values, 4), "areaStyle": {"opacity": 0.18}}
                for label, row in scaled.iterrows()
            ],
            "symbolSize": 5,
        }
    ]
    return option


def r_bullet(frame, f, o, ctx):
    x, actual, target = f["x"], f["y"], f["target"]
    data = d._labels(frame, [x])
    data[actual], data[target] = d._numeric(data, actual), d._numeric(data, target)
    grouped = (
        data.groupby(x, sort=False)[[actual, target]]
        .agg(o.get("aggregation", "sum"))
        .reset_index()
        .head(MAX_CATEGORIES)
    )
    attained = grouped[actual] >= grouped[target]
    option = base()
    option["tooltip"]["axisPointer"] = {"type": "shadow"}
    option["xAxis"] = value_axis("Value")
    option["yAxis"] = category_axis(grouped[x], inverse=True)
    option["series"] = [
        {
            "type": "bar",
            "name": f"{target} (target)",
            "data": nums(grouped[target]),
            "barWidth": "55%",
            "itemStyle": {"color": rgba(ctx["colors"][0], 0.16)},
            "z": 1,
        },
        {
            "type": "bar",
            "name": actual,
            "barGap": "-100%",
            "barWidth": "28%",
            "data": [
                {"value": num(v), "itemStyle": {"color": POSITIVE if hit else ctx["colors"][0]}}
                for v, hit in zip(grouped[actual], attained, strict=True)
            ],
            "z": 2,
        },
        {
            "type": "scatter",
            "name": "Target",
            "symbol": "rect",
            "symbolSize": [4, 26],
            "data": [[num(v), c] for c, v in zip(grouped[x], grouped[target], strict=True)],
            "itemStyle": {"color": ctx["tokens"]["fg"]},
            "z": 3,
        },
    ]
    _stat(ctx, "Targets met", f"{int(attained.sum())} of {len(grouped.index)}", "text")
    _insight(ctx, f"{int(attained.sum())} of {len(grouped.index)} {x} values reached their target.")
    return option


def r_pareto(frame, f, o, ctx):
    x, y = f["x"], f.get("y")
    data, value = d._aggregate(frame, [x], y, o.get("aggregation", "sum"))
    data = data.sort_values(value, ascending=False, kind="mergesort").head(
        int(o.get("top_n") or 0) or MAX_CATEGORIES
    )
    total = data[value].sum()
    cumulative = data[value].cumsum() / (total if total else 1) * 100
    option = base()
    option["tooltip"]["axisPointer"] = {"type": "shadow"}
    option["xAxis"] = category_axis(data[x])
    option["yAxis"] = [
        value_axis(d._value_label(y, o)),
        {
            "type": "value",
            "max": 100,
            "min": 0,
            "axisLabel": {"formatter": "{value}%"},
            "splitLine": {"show": False},
        },
    ]
    option["series"] = [
        {
            "type": "bar",
            "name": d._value_label(y, o),
            "data": nums(data[value]),
            "itemStyle": {"color": ctx["colors"][0], "borderRadius": [4, 4, 0, 0]},
        },
        {
            "type": "line",
            "name": "Cumulative share",
            "yAxisIndex": 1,
            "data": nums(cumulative, 2),
            "smooth": True,
            "lineStyle": {"width": 3},
            "itemStyle": {"color": ctx["colors"][2]},
            "markLine": {
                "silent": True,
                "symbol": "none",
                "data": [{"yAxis": 80}],
                "lineStyle": {"type": "dashed", "color": ctx["tokens"]["muted"]},
                "label": {"formatter": "80%"},
            },
        },
    ]
    vital = min(int((cumulative < 80).sum()) + 1, len(data.index))
    _stat(ctx, "Categories making up 80%", f"{vital} of {len(data.index)}", "text")
    _insight(
        ctx, f"{vital} of {len(data.index)} categories account for 80% of the total — focus there first."
    )
    return option


def _heatmap(
    xs: list[str],
    ys: list[str],
    cells: list[list[Any]],
    ctx: dict[str, Any],
    *,
    diverging: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
    show_values: bool = True,
    name: str = "",
) -> dict[str, Any]:
    values = [cell[2] for cell in cells if cell[2] is not None]
    low = minimum if minimum is not None else (min(values) if values else 0)
    high = maximum if maximum is not None else (max(values) if values else 1)
    option = base(trigger="item", legend=False)
    option["grid"]["right"] = 90
    option["xAxis"] = category_axis(
        xs, splitArea={"show": False}, axisLabel={"rotate": 30 if len(xs) > 8 else 0, "hideOverlap": True}
    )
    option["yAxis"] = category_axis(ys, inverse=True)
    option["visualMap"] = {
        "min": low,
        "max": high if high != low else low + 1,
        "calculable": True,
        "orient": "vertical",
        "right": 4,
        "top": "middle",
        "itemHeight": 160,
        "inRange": {"color": DIVERGING if diverging else ctx["seq"]},
    }
    option["series"] = [
        {
            "type": "heatmap",
            "name": name,
            "data": cells,
            "label": {
                "show": show_values and len(cells) <= 600,
                "formatter": fn("cellValue"),
                "fontSize": 11,
            },
            "itemStyle": {"borderColor": ctx["tokens"]["bg"], "borderWidth": 1},
            "emphasis": {"itemStyle": {"shadowBlur": 8}},
        }
    ]
    return option


def r_category_heatmap(frame, f, o, ctx):
    x, y, z = f["x"], f["y"], f.get("z")
    data, value = d._aggregate(frame, [y, x], z, o.get("aggregation", "sum"))
    pivot = data.pivot_table(index=y, columns=x, values=value, aggfunc="sum").iloc[
        :MAX_CATEGORIES, :MAX_CATEGORIES
    ]
    cells = [[j, i, num(pivot.iat[i, j], 4)] for i in range(pivot.shape[0]) for j in range(pivot.shape[1])]
    option = _heatmap(
        labels(pivot.columns),
        labels(pivot.index),
        cells,
        ctx,
        show_values=bool(o.get("show_values", True)),
        name=d._value_label(z, o),
    )
    stacked = pivot.stack()
    if len(stacked):
        best = stacked.idxmax()
        _insight(ctx, f"The highest value is {compact(float(stacked.max()))} for {best[0]} × {best[1]}.")
    return option


# --- Trends & time -----------------------------------------------------------------------------


def _trend_insight(ctx, frame, x, value):
    series = frame[[x, value]].dropna()
    if len(series.index) >= 2:
        first, last = float(series[value].iloc[0]), float(series[value].iloc[-1])
        change = (last - first) / abs(first) if first else float("nan")
        peak = series.loc[series[value].idxmax()]
        text = f"{value} went from {compact(first)} to {compact(last)}"
        text += f" ({change:+.0%})" if not math.isnan(change) else ""
        text += f"; the peak was {compact(float(peak[value]))} on {str(peak[x])[:10]}."
        _insight(ctx, text)


def _time_series_option(ctx, points: int) -> dict[str, Any]:
    option = base(zoom=points > 60)
    option["xAxis"] = time_axis()
    return option


def r_line(
    frame, f, o, ctx, step: bool = False, area: bool = False, stacked: bool = False, normalize: bool = False
):
    x, color = f["x"], f.get("color")
    frame = _limit_series(frame, color, ctx)
    data, values, _ = d._series_frame(frame, f, o)
    option = _time_series_option(ctx, len(data.index))
    option["yAxis"] = value_axis(
        values[0] if len(values) == 1 and not color else "Value", log=bool(o.get("log_y"))
    )
    groups = _groups_in_order(data, color, values[0]) if color else values
    if normalize and color:
        totals = data.groupby(x)[values[0]].transform("sum")
        data = data.assign(**{values[0]: data[values[0]] / totals.where(totals != 0) * 100})
        option["yAxis"].update({"max": 100, "axisLabel": {"formatter": "{value}%"}})
    for group in groups:
        part = data[data[color] == group] if color else data
        column = values[0] if color else group
        series = {
            "type": "line",
            "name": str(group),
            "data": [[stamp, num(v)] for stamp, v in zip(dates(part[x]), part[column], strict=True)],
            "showSymbol": bool(o.get("markers")),
            "symbolSize": 6,
            "lineStyle": {"width": 2.4},
            "emphasis": {"focus": "series"},
        }
        if step:
            series["step"] = "end"
        if area or stacked:
            series["areaStyle"] = {"opacity": 0.22 if not stacked else 0.55}
        if stacked:
            series["stack"] = "total"
        option["series"].append(series)
    option["legend"]["show"] = len(groups) > 1
    if not color:
        _trend_insight(ctx, data, x, values[0])
    return option


def r_step_line(frame, f, o, ctx):
    return r_line(frame, f, o, ctx, step=True)


def r_area(frame, f, o, ctx):
    return r_line(frame, f, o, ctx, area=True)


def r_stacked_area(frame, f, o, ctx):
    return r_line(frame, f, o, ctx, stacked=True, normalize=bool(o.get("normalize")))


def r_rolling_average(frame, f, o, ctx):
    data, values, _ = d._series_frame(frame, f, o)
    value, x = values[0], f["x"]
    window = int(o.get("window", 7))
    rolling = data[value].rolling(window, min_periods=max(1, window // 2)).mean()
    stamps = dates(data[x])
    option = _time_series_option(ctx, len(data.index))
    option["yAxis"] = value_axis(value)
    option["series"] = [
        {
            "type": "line",
            "name": value,
            "data": [[s, num(v)] for s, v in zip(stamps, data[value], strict=True)],
            "showSymbol": False,
            "lineStyle": {"width": 1.2, "opacity": 0.45},
            "itemStyle": {"color": ctx["colors"][1]},
        },
        {
            "type": "line",
            "name": f"{window}-point average",
            "data": [[s, num(v)] for s, v in zip(stamps, rolling, strict=True)],
            "showSymbol": False,
            "smooth": True,
            "lineStyle": {"width": 3.2},
            "itemStyle": {"color": ctx["colors"][0]},
        },
    ]
    _trend_insight(ctx, data.assign(**{value: rolling}), x, value)
    return option


def r_cumulative(frame, f, o, ctx):
    color = f.get("color")
    frame = _limit_series(frame, color, ctx)
    data, values, _ = d._series_frame(frame, f, o)
    value, x = values[0], f["x"]
    data["Running total"] = data.groupby(color)[value].cumsum() if color else data[value].cumsum()
    option = _time_series_option(ctx, len(data.index))
    option["yAxis"] = value_axis("Running total")
    groups = _groups_in_order(data, color, value) if color else [None]
    for group in groups:
        part = data[data[color] == group] if color else data
        option["series"].append(
            {
                "type": "line",
                "name": group or "Running total",
                "data": [[s, num(v)] for s, v in zip(dates(part[x]), part["Running total"], strict=True)],
                "showSymbol": False,
                "areaStyle": {"opacity": 0.18} if not color else None,
                "lineStyle": {"width": 2.6},
            }
        )
    option["legend"]["show"] = bool(color)
    if not color and len(data.index):
        _insight(
            ctx,
            (
                f"The running total reaches {compact(float(data['Running total'].iloc[-1]))} by "
                f"{str(data[x].iloc[-1])[:10]}."
            ),
        )
    return option


def r_period_change(frame, f, o, ctx):
    data, values, _ = d._series_frame(frame, f, o)
    value, x = values[0], f["x"]
    change = data[value].pct_change().replace([math.inf, -math.inf], math.nan) * 100
    option = _time_series_option(ctx, len(data.index))
    option["legend"]["show"] = False
    option["yAxis"] = {
        "type": "value",
        "name": "Change from previous period",
        "nameLocation": "middle",
        "nameGap": 48,
        "axisLabel": {"formatter": "{value}%"},
    }
    option["series"] = [
        {
            "type": "bar",
            "name": "Change",
            "data": [
                {"value": [s, num(v, 2)], "itemStyle": {"color": POSITIVE if (v or 0) >= 0 else NEGATIVE}}
                for s, v in zip(dates(data[x]), change, strict=True)
            ],
            "barMaxWidth": 28,
        }
    ]
    option["tooltip"]["valueFormatter"] = fn("percent", digits=1)
    if change.notna().any():
        _stat(ctx, "Average change", round(float(change.mean()), 2), "percent_value")
        _insight(
            ctx,
            (
                f"On average {value} changes {change.mean():+.1f}% per period; "
                f"{int((change > 0).sum())} of {int(change.notna().sum())} periods grew."
            ),
        )
    return option


def r_candlestick(frame, f, o, ctx):
    x = f["x"]
    data = frame[[x, f["open"], f["high"], f["low"], f["close"]]].copy()
    data[x] = d._dates(data[x])
    for key in ("open", "high", "low", "close"):
        data[f[key]] = d._numeric(data, f[key])
    data = d._sample(data.dropna().sort_values(x), 3000, ctx, "candles").sort_values(x)
    option = base(zoom=True)
    option["tooltip"]["axisPointer"] = {"type": "cross"}
    option["xAxis"] = category_axis(dates(data[x]), boundaryGap=True)
    option["yAxis"] = value_axis("Price", scale=True)
    option["series"] = [
        {
            "type": "candlestick",
            "name": "Price",
            "data": [
                [num(r[f["open"]]), num(r[f["close"]]), num(r[f["low"]]), num(r[f["high"]])]
                for _, r in data.iterrows()
            ],
            "itemStyle": {
                "color": POSITIVE,
                "color0": NEGATIVE,
                "borderColor": POSITIVE,
                "borderColor0": NEGATIVE,
            },
        }
    ]
    if len(data.index) >= 2:
        first, last = float(data[f["close"]].iloc[0]), float(data[f["close"]].iloc[-1])
        _insight(
            ctx,
            (
                f"Closing price moved {((last - first) / first if first else 0):+.1%} over the period "
                f"({compact(first)} → {compact(last)})."
            ),
        )
    return option


def r_range_band(frame, f, o, ctx):
    x = f["x"]
    data = frame[[x, f["y"], f["lower"], f["upper"]]].copy()
    data[x] = d._dates(data[x])
    for key in ("y", "lower", "upper"):
        data[f[key]] = d._numeric(data, f[key])
    data = data.dropna().sort_values(x)
    stamps = dates(data[x])
    color = ctx["colors"][0]
    option = _time_series_option(ctx, len(data.index))
    option["yAxis"] = value_axis(f["y"], scale=True)
    option["series"] = [
        {
            "type": "line",
            "name": f["lower"],
            "data": [[s, num(v)] for s, v in zip(stamps, data[f["lower"]], strict=True)],
            "stack": "band",
            "showSymbol": False,
            "lineStyle": {"opacity": 0},
            "tooltip": {"show": False},
        },
        {
            "type": "line",
            "name": "Range",
            "data": [
                [s, num(u - lo)] for s, u, lo in zip(stamps, data[f["upper"]], data[f["lower"]], strict=True)
            ],
            "stack": "band",
            "showSymbol": False,
            "lineStyle": {"opacity": 0},
            "areaStyle": {"color": rgba(color, 0.22)},
            "tooltip": {"show": False},
        },
        {
            "type": "line",
            "name": f["y"],
            "data": [[s, num(v)] for s, v in zip(stamps, data[f["y"]], strict=True)],
            "showSymbol": False,
            "lineStyle": {"width": 2.8},
            "itemStyle": {"color": color},
        },
    ]
    option["legend"]["data"] = [f["y"], "Range"]
    return option


def r_calendar_heatmap(frame, f, o, ctx):
    import pandas as pd

    x, y = f["x"], f.get("y")
    data, values, _ = d._time_aggregate(frame, x, [y] if y else [], [], o.get("aggregation", "sum"), "D")
    value = values[0]
    end = data[x].max()
    data = data[data[x] > end - pd.Timedelta(days=365)]
    option = base(trigger="item", legend=False)
    option.pop("grid")
    option["calendar"] = {
        "range": [str(data[x].min().date()), str(end.date())],
        "cellSize": ["auto", 18],
        "top": 56,
        "left": 40,
        "right": 20,
        "itemStyle": {"borderColor": ctx["tokens"]["bg"], "borderWidth": 2, "color": ctx["tokens"]["grid"]},
        "splitLine": {"show": False},
        "dayLabel": {"color": ctx["tokens"]["muted"], "firstDay": 1},
        "monthLabel": {"color": ctx["tokens"]["muted"]},
        "yearLabel": {"show": False},
    }
    series_values = data[value].dropna()
    option["visualMap"] = {
        "min": num(series_values.min()) or 0,
        "max": num(series_values.max()) or 1,
        "orient": "horizontal",
        "left": "center",
        "top": 0,
        "calculable": True,
        "inRange": {"color": ctx["seq"]},
    }
    option["series"] = [
        {
            "type": "heatmap",
            "coordinateSystem": "calendar",
            "name": value,
            "data": [[str(s.date()), num(v)] for s, v in zip(data[x], data[value], strict=True)],
        }
    ]
    if len(data.index):
        weekday = data.assign(_day=data[x].dt.day_name()).groupby("_day")[value].mean()
        _insight(
            ctx,
            (
                f"{weekday.idxmax()} is the strongest day on average ({compact(float(weekday.max()))}); "
                f"{weekday.idxmin()} the weakest."
            ),
        )
    return option


def r_seasonal_decompose(frame, f, o, ctx):
    from statsmodels.tsa.seasonal import seasonal_decompose

    data, values, grain = d._series_frame(frame, f, o, [f["y"]])
    period = SEASON.get(grain)
    if not period:
        raise AnalysisError("Choose day, week, month or quarter grouping for a seasonal decomposition.")
    series = data.set_index(f["x"])[values[0]].asfreq(GRAIN_FREQ[grain]).interpolate(limit_direction="both")
    if len(series.dropna()) < 2 * period:
        raise AnalysisError(
            f"A seasonal decomposition needs at least {2 * period} periods; found {len(series.dropna())}."
        )
    model = o.get("model", "additive")
    if model == "multiplicative" and (series <= 0).any():
        raise AnalysisError("A multiplicative decomposition needs strictly positive values.")
    result = seasonal_decompose(series, model=model, period=period)
    parts = (
        ("Observed", result.observed),
        ("Trend", result.trend),
        ("Seasonal", result.seasonal),
        ("Residual", result.resid),
    )
    stamps = dates(series.index.to_series())
    grids = panels(4, 1, top=6, gap=5)
    option = base(legend=False)
    option["grid"] = grids
    option["xAxis"] = [time_axis(gridIndex=i, axisLabel={"show": i == 3}) for i in range(4)]
    option["yAxis"] = [value_axis("", gridIndex=i, scale=True, splitNumber=3) for i in range(4)]
    option["title"] = [
        {"text": name, "left": "3%", "top": f"{float(grids[i]['top'][:-1]) - 4:.1f}%"}
        for i, (name, _) in enumerate(parts)
    ]
    option["dataZoom"] = [{"type": "inside", "xAxisIndex": [0, 1, 2, 3]}]
    option["axisPointer"] = {"link": [{"xAxisIndex": "all"}]}
    for index, (name, part) in enumerate(parts):
        option["series"].append(
            {
                "type": "scatter" if index == 3 else "line",
                "name": name,
                "xAxisIndex": index,
                "yAxisIndex": index,
                "data": [[s, num(v)] for s, v in zip(stamps, part.values, strict=True)],
                "showSymbol": False,
                "symbolSize": 4,
                "itemStyle": {"color": ctx["colors"][index % len(ctx["colors"])]},
                "lineStyle": {"width": 2},
            }
        )
    combined = (result.seasonal + result.resid).var()
    strength = 1 - float(result.resid.var() / combined) if combined else 0
    _stat(ctx, "Seasonal strength", round(max(0.0, strength), 3))
    _insight(
        ctx,
        (
            "Seasonality explains "
            f"{'a lot' if strength > 0.6 else 'some' if strength > 0.3 else 'little'} of the variation "
            f"(strength {max(0.0, strength):.2f}); the trend line shows the underlying direction."
        ),
    )
    return option


def r_autocorrelation(frame, f, o, ctx):
    import numpy as np
    from statsmodels.tsa.stattools import acf, pacf

    data, values, _ = d._series_frame(frame, f, o, [f["y"]])
    series = data[values[0]].dropna()
    if len(series) < 10:
        raise AnalysisError("Autocorrelation needs at least 10 periods.")
    lags = int(min(o.get("lags", 24), len(series) // 2 - 1))
    bound = float(1.96 / np.sqrt(len(series)))
    grids = panels(2, 1, top=8, gap=12)
    option = base(legend=False)
    option["grid"] = grids
    option["title"] = [
        {"text": "Autocorrelation (ACF)", "left": "3%", "top": "1%"},
        {
            "text": "Partial autocorrelation (PACF)",
            "left": "3%",
            "top": f"{float(grids[1]['top'][:-1]) - 5:.1f}%",
        },
    ]
    option["xAxis"] = [
        category_axis(list(range(lags + 1)), "Lag" if i == 1 else "", gridIndex=i) for i in range(2)
    ]
    option["yAxis"] = [value_axis("", gridIndex=i, min=-1, max=1) for i in range(2)]
    significant = 0
    for index, values_ in enumerate((acf(series, nlags=lags), pacf(series, nlags=lags))):
        significant += int(sum(abs(v) > bound for v in values_[1:])) if index == 0 else 0
        option["series"].append(
            {
                "type": "bar",
                "xAxisIndex": index,
                "yAxisIndex": index,
                "data": [
                    {
                        "value": num(v, 4),
                        "itemStyle": {"color": ctx["colors"][0] if abs(v) > bound else ctx["tokens"]["line"]},
                    }
                    for v in values_
                ],
                "markLine": {
                    "silent": True,
                    "symbol": "none",
                    "lineStyle": {"type": "dashed", "color": NEGATIVE},
                    "data": [{"yAxis": num(bound, 4)}, {"yAxis": num(-bound, 4)}],
                    "label": {"show": False},
                },
            }
        )
    _insight(
        ctx,
        (
            f"{significant} of {lags} lags are significant: values above the red dashed band mean the "
            "series repeats patterns at that lag."
        ),
    )
    return option


# --- Composition -------------------------------------------------------------------------------


def r_pie(frame, f, o, ctx, hole: float = 0.0):
    x, y = f["x"], f.get("y")
    data, value = d._aggregate(frame, [x], y, o.get("aggregation", "sum"))
    if (data[value] < 0).any():
        raise AnalysisError("Pie charts cannot show negative values; use a bar or waterfall chart.")
    data = d._with_other(data, x, value, int(o.get("top_n") or 0) or 10)
    total = float(data[value].sum())
    option = base(trigger="item")
    option.pop("grid")
    option["legend"].update({"type": "scroll", "bottom": 0, "top": None})
    option["series"] = [
        {
            "type": "pie",
            "name": d._value_label(y, o),
            "radius": [f"{hole * 72:.0f}%", "72%"],
            "center": ["50%", "48%"],
            "data": [{"name": str(n), "value": num(v)} for n, v in zip(data[x], data[value], strict=True)],
            "itemStyle": {
                "borderColor": ctx["tokens"]["bg"],
                "borderWidth": 2,
                "borderRadius": 6 if hole else 0,
            },
            "label": {"formatter": "{b}\n{d}%", "color": ctx["tokens"]["fg"]},
            "emphasis": {"scale": True, "scaleSize": 6},
        }
    ]
    if hole:
        option["title"] = {
            "text": compact(total),
            "subtext": "Total",
            "left": "center",
            "top": "42%",
            "textStyle": {"fontSize": 22},
        }
    if len(data.index) and total:
        _insight(
            ctx,
            (
                f"{data[x].iloc[0]} is the largest slice at {data[value].iloc[0] / total:.0%} of "
                f"{compact(total)}."
            ),
        )
    return option


def r_donut(frame, f, o, ctx):
    return r_pie(frame, f, o, ctx, hole=float(o.get("hole", 0.55)))


def _tree(data: Any, path: list[str], value: str) -> list[dict[str, Any]]:
    nodes = []
    for name, group in data.groupby(path[0], sort=False):
        node: dict[str, Any] = {"name": str(name), "value": num(group[value].sum())}
        if len(path) > 1:
            node["children"] = _tree(group, path[1:], value)
        nodes.append(node)
    return sorted(nodes, key=lambda node: -(node["value"] or 0))


def _hierarchy_data(frame, f, o, ctx):
    path, y = f["path"][:5], f.get("y")
    data, value = d._aggregate(frame, path, y, o.get("aggregation", "sum"))
    data = data[data[value] > 0]
    if data.empty:
        raise AnalysisError("Hierarchy charts need positive values.")
    if len(data.index) > 2000:
        data = data.nlargest(2000, value)
        ctx["notes"].append("Showing the 2,000 largest branches.")
    top = data.groupby(path[0])[value].sum().sort_values(ascending=False)
    _insight(ctx, f"{top.index[0]} is the biggest {path[0]} ({top.iloc[0] / top.sum():.0%} of the total).")
    return data, path, value


def r_treemap(frame, f, o, ctx):
    data, path, value = _hierarchy_data(frame, f, o, ctx)
    option = base(trigger="item", legend=False)
    option.pop("grid")
    option["series"] = [
        {
            "type": "treemap",
            "name": "All",
            "data": _tree(data, path, value),
            "top": 36,
            "left": 8,
            "right": 8,
            "bottom": 28,
            "roam": False,
            "breadcrumb": {"show": True, "bottom": 2},
            "label": {"show": True, "formatter": "{b}"},
            "upperLabel": {"show": True, "height": 22, "color": "#fff"},
            "levels": [
                {"itemStyle": {"borderColor": ctx["tokens"]["bg"], "borderWidth": 3, "gapWidth": 3}},
                {
                    "colorSaturation": [0.35, 0.6],
                    "itemStyle": {"borderColorSaturation": 0.7, "gapWidth": 2, "borderWidth": 2},
                },
                {"colorSaturation": [0.3, 0.5], "itemStyle": {"gapWidth": 1}},
            ],
        }
    ]
    return option


def r_sunburst(frame, f, o, ctx):
    data, path, value = _hierarchy_data(frame, f, o, ctx)
    option = base(trigger="item", legend=False)
    option.pop("grid")
    option["series"] = [
        {
            "type": "sunburst",
            "data": _tree(data, path, value),
            "radius": [0, "92%"],
            "center": ["50%", "52%"],
            "sort": None,
            "itemStyle": {"borderColor": ctx["tokens"]["bg"], "borderWidth": 2, "borderRadius": 4},
            "label": {"rotate": "radial", "minAngle": 8, "color": "#fff"},
            "emphasis": {"focus": "ancestor"},
        }
    ]
    return option


def r_icicle(frame, f, o, ctx):
    data, path, value = _hierarchy_data(frame, f, o, ctx)
    total = float(data[value].sum())
    items: list[dict[str, Any]] = []

    def place(subset, depth: int, start: float, prefix: str) -> None:
        cursor = start
        for name, group in sorted(
            subset.groupby(path[depth], sort=False), key=lambda item: -item[1][value].sum()
        ):
            width = float(group[value].sum()) / total
            color = ctx["colors"][len(items) % len(ctx["colors"])] if depth == 0 else None
            label = f"{prefix}{name}"
            shapes = [
                {
                    "type": "polygon",
                    "points": [
                        [cursor, depth],
                        [cursor + width, depth],
                        [cursor + width, depth + 1],
                        [cursor, depth + 1],
                    ],
                    "stroke": ctx["tokens"]["bg"],
                    "strokeWidth": 2,
                    "opacity": 0.92 - depth * 0.12,
                    **({"color": color} if color else {}),
                }
            ]
            if width > 0.04:
                shapes.append(
                    {"type": "text", "points": [[cursor + width / 2, depth + 0.5]], "text": str(name)[:24]}
                )
            items.append(
                {
                    "name": label,
                    "value": [cursor + width / 2, depth + 0.5, num(group[value].sum())],
                    "shapes": shapes,
                }
            )
            if depth + 1 < len(path):
                place(group, depth + 1, cursor, f"{label} › ")
            cursor += width

    place(data, 0, 0.0, "")
    option = base(trigger="item", legend=False)
    option["tooltip"]["formatter"] = fn("shapeTooltip")
    option["xAxis"] = {"type": "value", "min": 0, "max": 1, "show": False}
    option["yAxis"] = {"type": "value", "min": 0, "max": len(path), "inverse": True, "show": False}
    option["series"] = [shapes_series(items, name="Hierarchy", colorBy="data")]
    return option


def r_waterfall(frame, f, o, ctx):
    x, y = f["x"], f["y"]
    data, value = d._aggregate(frame, [x], y, "sum")
    data = data.head(MAX_CATEGORIES)
    steps, bases, ups, downs, running = labels(data[x]), [], [], [], 0.0
    for delta in data[value].astype(float):
        if delta >= 0:
            bases.append(num(running))
            ups.append(num(delta))
            downs.append(None)
        else:
            bases.append(num(running + delta))
            ups.append(None)
            downs.append(num(-delta))
        running += delta
    if o.get("total", True):
        steps.append("Total")
        bases.append(0 if running >= 0 else num(running))
        ups.append(None)
        downs.append(None)
    totals = [None] * (len(steps) - 1) + [num(abs(running))] if o.get("total", True) else [None] * len(steps)
    option = base()
    option["tooltip"]["axisPointer"] = {"type": "shadow"}
    option["xAxis"] = category_axis(steps)
    option["yAxis"] = value_axis(y)
    option["series"] = [
        {
            "type": "bar",
            "name": "Base",
            "stack": "w",
            "data": bases,
            "itemStyle": {"color": "transparent"},
            "emphasis": {"disabled": True},
            "tooltip": {"show": False},
        },
        {
            "type": "bar",
            "name": "Increase",
            "stack": "w",
            "data": ups,
            "itemStyle": {"color": POSITIVE, "borderRadius": 3},
            "label": {"show": True, "position": "top", "formatter": fn("labelValue")},
        },
        {
            "type": "bar",
            "name": "Decrease",
            "stack": "w",
            "data": downs,
            "itemStyle": {"color": NEGATIVE, "borderRadius": 3},
            "label": {"show": True, "position": "bottom", "formatter": fn("labelValue")},
        },
        {
            "type": "bar",
            "name": "Total",
            "stack": "w",
            "data": totals,
            "itemStyle": {"color": ctx["colors"][0], "borderRadius": 3},
            "label": {"show": True, "position": "top", "formatter": fn("labelValue")},
        },
    ]
    option["legend"]["data"] = ["Increase", "Decrease", "Total"]
    _insight(
        ctx,
        (
            f"The steps add up to {compact(running)}; the biggest move is "
            f"{steps[int(data[value].abs().values.argmax())]} "
            f"({compact(float(data[value].iloc[int(data[value].abs().values.argmax())]))})."
        ),
    )
    return option


def r_funnel(frame, f, o, ctx):
    x, y = f["x"], f.get("y")
    data, value = d._aggregate(frame, [x], y, o.get("aggregation", "sum"))
    if o.get("sort", "desc") == "desc":
        data = data.sort_values(value, ascending=False, kind="mergesort")
    data = data.head(MAX_CATEGORIES)
    option = base(trigger="item")
    option.pop("grid")
    option["series"] = [
        {
            "type": "funnel",
            "name": d._value_label(y, o),
            "left": "8%",
            "right": "8%",
            "top": 40,
            "bottom": 10,
            "sort": "none" if o.get("sort", "desc") != "desc" else "descending",
            "gap": 3,
            "label": {"show": True, "position": "inside", "formatter": "{b}: {c}", "color": "#fff"},
            "itemStyle": {"borderColor": ctx["tokens"]["bg"], "borderWidth": 1},
            "data": [{"name": str(n), "value": num(v)} for n, v in zip(data[x], data[value], strict=True)],
        }
    ]
    if len(data.index) >= 2 and data[value].iloc[0]:
        _insight(
            ctx,
            (
                f"{data[value].iloc[-1] / data[value].iloc[0]:.1%} of {data[x].iloc[0]} reach "
                f"{data[x].iloc[-1]}."
            ),
        )
    return option


def r_marimekko(frame, f, o, ctx):
    x, segment, y = f["x"], f["color"], f.get("y")
    frame = _limit_series(frame, segment, ctx, y)
    data, value = d._aggregate(frame, [x, segment], y, o.get("aggregation", "sum"))
    pivot = data.pivot_table(index=x, columns=segment, values=value, aggfunc="sum", fill_value=0)
    pivot = pivot.loc[pivot.sum(axis=1).sort_values(ascending=False).index[:20]]
    widths = pivot.sum(axis=1)
    total = float(widths.sum()) or 1.0
    left = 0.0
    series = {str(column): [] for column in pivot.columns}
    for name, row in pivot.iterrows():
        width = float(widths[name]) / total * 100
        bottom = 0.0
        for index, column in enumerate(pivot.columns):
            share = float(row[column]) / float(widths[name]) * 100 if widths[name] else 0
            color = ctx["colors"][index % len(ctx["colors"])]
            shapes = [
                {
                    "type": "polygon",
                    "points": [
                        [left, bottom],
                        [left + width, bottom],
                        [left + width, bottom + share],
                        [left, bottom + share],
                    ],
                    "color": color,
                    "stroke": ctx["tokens"]["bg"],
                    "strokeWidth": 1,
                    "opacity": 0.9,
                }
            ]
            if width > 6 and share > 8:
                shapes.append(
                    {
                        "type": "text",
                        "points": [[left + width / 2, bottom + share / 2]],
                        "text": f"{share:.0f}%",
                    }
                )
            series[str(column)].append(
                {
                    "name": f"{name} · {column}",
                    "value": [left + width / 2, bottom + share / 2, num(row[column])],
                    "shapes": shapes,
                }
            )
            bottom += share
        left += width
    option = base(trigger="item")
    option["tooltip"]["formatter"] = fn("shapeTooltip")
    option["xAxis"] = {
        "type": "value",
        "min": 0,
        "max": 100,
        "name": f"{x} (width = share of total)",
        "nameLocation": "middle",
        "nameGap": 28,
        "axisLabel": {"formatter": "{value}%"},
    }
    option["yAxis"] = {"type": "value", "min": 0, "max": 100, "axisLabel": {"formatter": "{value}%"}}
    option["series"] = [
        shapes_series(items, name=name, itemStyle={"color": ctx["colors"][i % len(ctx["colors"])]})
        for i, (name, items) in enumerate(series.items())
    ]
    return option


# --- Distribution ------------------------------------------------------------------------------


def _bins(values: Any, bins: int | None) -> Any:
    import numpy as np

    edges = np.histogram_bin_edges(values, bins=bins or "auto")
    if len(edges) > 81:
        edges = np.histogram_bin_edges(values, bins=80)
    return edges


def _bin_labels(edges: Any) -> list[str]:
    return [f"{compact(float(edges[i]))}–{compact(float(edges[i + 1]))}" for i in range(len(edges) - 1)]


def r_histogram(frame, f, o, ctx):
    import numpy as np

    x = f["x"]
    data, color = d._color_groups(frame, f.get("color"), int(ctx.get("max_series") or 12))
    data = data.assign(**{x: d._numeric(data, x)}).dropna(subset=[x])
    values = data[x].to_numpy(dtype=float)
    if not len(values):
        raise AnalysisError(f"{x} has no numeric values.")
    edges = _bins(values, int(o.get("bins") or 0) or None)
    norm = o.get("histnorm") or ""
    groups = list(data.groupby(color)) if color else [(x, data)]
    option = base()
    option["tooltip"]["axisPointer"] = {"type": "shadow"}
    marginal = o.get("marginal") == "box"
    if marginal:
        option["grid"] = [
            {"left": 16, "right": 28, "top": 40, "height": 56, "containLabel": True},
            {"left": 16, "right": 28, "top": 120, "bottom": 16, "containLabel": True},
        ]
    main = 1 if marginal else 0
    option["xAxis"] = [
        category_axis(_bin_labels(edges), x, gridIndex=main, axisLabel={"rotate": 35, "hideOverlap": True})
    ]
    option["yAxis"] = [
        value_axis(
            {"percent": "Percent", "probability density": "Density"}.get(norm, "Count"), gridIndex=main
        )
    ]
    for label, group in groups:
        counts, _ = np.histogram(group[x].to_numpy(dtype=float), bins=edges)
        if norm == "percent":
            counts = counts / max(counts.sum(), 1) * 100
        elif norm == "probability density":
            counts = counts / max(counts.sum(), 1) / np.diff(edges)
        option["series"].append(
            {
                "type": "bar",
                "name": str(label),
                "data": nums(counts, 6),
                "barCategoryGap": "4%",
                "barGap": "-100%" if color else "10%",
                "itemStyle": {"opacity": 0.7 if color else 0.95, "borderRadius": [3, 3, 0, 0]},
                "xAxisIndex": len(option["xAxis"]) - 1,
                "yAxisIndex": len(option["yAxis"]) - 1,
            }
        )
    if marginal:
        q1, med, q3 = np.percentile(values, [25, 50, 75])
        iqr = q3 - q1
        low, high = values[values >= q1 - 1.5 * iqr].min(), values[values <= q3 + 1.5 * iqr].max()
        option["xAxis"].insert(
            0,
            value_axis("", gridIndex=0, min=float(edges[0]), max=float(edges[-1]), axisLabel={"show": False}),
        )
        option["yAxis"].insert(
            0, {"type": "category", "data": [""], "gridIndex": 0, "axisTick": {"show": False}}
        )
        for series in option["series"]:
            series["xAxisIndex"], series["yAxisIndex"] = 1, 1
        option["series"].append(
            {
                "type": "boxplot",
                "name": "Spread",
                "xAxisIndex": 0,
                "yAxisIndex": 0,
                "data": [nums([low, q1, med, q3, high])],
                "itemStyle": {"color": rgba(ctx["colors"][0], 0.2), "borderColor": ctx["colors"][0]},
            }
        )
    option["legend"]["show"] = bool(color)
    series = data[x]
    _stat(ctx, "Mean", round(float(series.mean()), 4))
    _stat(ctx, "Median", round(float(series.median()), 4))
    _stat(ctx, "Std. deviation", round(float(series.std()), 4) if len(series) > 1 else 0)
    skew = float(series.skew()) if len(series) > 2 else 0.0
    _stat(ctx, "Skewness", round(skew, 3))
    shape = (
        "roughly symmetric"
        if abs(skew) < 0.5
        else (
            "right-skewed (a long tail of high values)"
            if skew > 0
            else "left-skewed (a long tail of low values)"
        )
    )
    _insight(
        ctx, f"Most {x} values sit around {compact(float(series.median()))}; the distribution is {shape}."
    )
    return option


def r_density_kde(frame, f, o, ctx):
    import numpy as np
    from scipy.stats import gaussian_kde

    x = f["x"]
    data, color = d._color_groups(frame, f.get("color"), 10)
    data = data.assign(**{x: d._numeric(data, x)}).dropna(subset=[x])
    groups = list(data.groupby(color)) if color else [(x, data)]
    low, high = data[x].min(), data[x].max()
    pad = (high - low) * 0.1 or 1
    grid = np.linspace(low - pad, high + pad, 240)
    option = base()
    option["xAxis"] = value_axis(x, scale=True)
    option["yAxis"] = value_axis("Density")
    peaks = []
    for label, group in groups:
        values = group[x].to_numpy()
        if len(values) < 3 or np.ptp(values) == 0:
            continue
        density = gaussian_kde(values, bw_method=o.get("bandwidth", "scott"))(grid)
        peaks.append((str(label), float(grid[density.argmax()])))
        option["series"].append(
            {
                "type": "line",
                "name": str(label),
                "data": [[num(a), num(b, 8)] for a, b in zip(grid, density, strict=True)],
                "smooth": True,
                "showSymbol": False,
                "areaStyle": {"opacity": 0.2},
                "lineStyle": {"width": 2.4},
            }
        )
    if not option["series"]:
        raise AnalysisError("A density curve needs at least three distinct values per group.")
    option["legend"]["show"] = bool(color)
    _insight(
        ctx, "Most common values: " + ", ".join(f"{name} ≈ {compact(peak)}" for name, peak in peaks[:4]) + "."
    )
    return option


def _five(values: Any) -> tuple[list[float | None], list[float]]:
    import numpy as np

    q1, med, q3 = np.percentile(values, [25, 50, 75])
    iqr = q3 - q1
    inside = values[(values >= q1 - 1.5 * iqr) & (values <= q3 + 1.5 * iqr)]
    low, high = (inside.min(), inside.max()) if len(inside) else (q1, q3)
    outliers = values[(values < q1 - 1.5 * iqr) | (values > q3 + 1.5 * iqr)]
    return nums([low, q1, med, q3, high]), [float(v) for v in outliers[:2000]]


def r_box(frame, f, o, ctx):
    data, x, y, color = d._grouped_axes(frame, f, ctx)
    groups = labels(sorted(data[x].unique())) if x else [y]
    colors = _groups_in_order(data, color) if color else [None]
    option = base(trigger="item")
    option["xAxis"] = category_axis(groups, x or "")
    option["yAxis"] = value_axis(y, scale=True)
    points = o.get("points", "outliers")
    medians = {}
    for index, colour in enumerate(colors):
        part = data if colour is None else data[data[color] == colour]
        boxes, scatter = [], []
        for group in groups:
            values = (part[part[x] == group] if x else part)[y].to_numpy(dtype=float)
            if not len(values):
                boxes.append([])
                continue
            box, outliers = _five(values)
            boxes.append(box)
            medians[group] = box[2]
            chosen = values if points == "all" else (outliers if points == "outliers" else [])
            scatter += [[group, num(v)] for v in list(chosen)[:3000]]
        option["series"].append(
            {
                "type": "boxplot",
                "name": colour or y,
                "data": boxes,
                "itemStyle": {
                    "color": rgba(ctx["colors"][index % len(ctx["colors"])], 0.25),
                    "borderColor": ctx["colors"][index % len(ctx["colors"])],
                    "borderWidth": 1.6,
                },
            }
        )
        if scatter:
            option["series"].append(
                {
                    "type": "scatter",
                    "name": f"{colour or y} points",
                    "data": scatter,
                    "symbolSize": 5,
                    "itemStyle": {"color": ctx["colors"][index % len(ctx["colors"])], "opacity": 0.45},
                }
            )
    option["legend"]["show"] = bool(color)
    if len(medians) > 1:
        best = max(medians, key=lambda key: medians[key] or 0)
        _insight(
            ctx,
            (
                f"{best} has the highest median {y} ({compact(medians[best] or 0)}); boxes show the "
                "middle 50% and dots are outliers."
            ),
        )
    elif medians:
        _insight(
            ctx,
            (
                f"The median {y} is {compact(next(iter(medians.values())) or 0)}; the box spans the "
                "middle 50% of values."
            ),
        )
    return option


def _kde_shape(values: Any, grid: Any) -> Any:
    import numpy as np
    from scipy.stats import gaussian_kde

    if len(values) < 3 or np.ptp(values) == 0:
        return np.zeros_like(grid)
    density = gaussian_kde(values)(grid)
    return density / density.max()


def r_violin(frame, f, o, ctx):
    import numpy as np

    data, x, y, color = d._grouped_axes(frame, f, ctx)
    groups = labels(sorted(data[x].unique())) if x else [y]
    low, high = float(data[y].min()), float(data[y].max())
    pad = (high - low) * 0.05 or 1
    grid = np.linspace(low - pad, high + pad, 80)
    items, points = [], []
    rng = np.random.default_rng(0)
    for index, group in enumerate(groups):
        values = (data[data[x] == group] if x else data)[y].to_numpy(dtype=float)
        if not len(values):
            continue
        width = _kde_shape(values, grid) * 0.42
        outline = [[index - w, v] for w, v in zip(width, grid, strict=True)] + [
            [index + w, v] for w, v in zip(width[::-1], grid[::-1], strict=True)
        ]
        color_ = ctx["colors"][index % len(ctx["colors"])]
        shapes = [
            {
                "type": "polygon",
                "points": [[num(px, 4), num(py, 6)] for px, py in outline],
                "color": color_,
                "opacity": 0.45,
                "stroke": color_,
                "strokeWidth": 1.2,
            }
        ]
        if o.get("box", True):
            q1, med, q3 = np.percentile(values, [25, 50, 75])
            shapes += [
                {
                    "type": "polygon",
                    "points": [
                        [index - 0.05, q1],
                        [index + 0.05, q1],
                        [index + 0.05, q3],
                        [index - 0.05, q3],
                    ],
                    "color": ctx["tokens"]["fg"],
                    "opacity": 0.75,
                },
                {
                    "type": "polyline",
                    "points": [[index - 0.12, med], [index + 0.12, med]],
                    "color": ctx["tokens"]["bg"],
                    "width": 2.5,
                },
            ]
        items.append({"name": group, "value": [index, num(np.median(values))], "shapes": shapes})
        mode = o.get("points", "outliers")
        if mode != "none":
            chosen = values if mode == "all" else _five(values)[1]
            points += [[num(index + rng.uniform(-0.18, 0.18), 4), num(v)] for v in list(chosen)[:2000]]
    option = base(trigger="item", legend=False)
    option["xAxis"] = index_axis(groups, x or "")
    option["yAxis"] = value_axis(y, min=num(low - pad), max=num(high + pad))
    option["series"] = [shapes_series(items, name=y, tooltip={"formatter": fn("shapeTooltip")})]
    if points:
        option["series"].append(
            {
                "type": "scatter",
                "name": "Points",
                "data": points,
                "symbolSize": 4,
                "itemStyle": {"color": ctx["tokens"]["muted"], "opacity": 0.5},
            }
        )
    _insight(
        ctx,
        (
            "Wider sections mean more values at that level; the dark bar is the middle 50% and the "
            "light line is the median."
        ),
    )
    return option


def r_strip(frame, f, o, ctx):
    import numpy as np

    data, x, y, color = d._grouped_axes(frame, f, ctx)
    data = d._sample(data, 5000, ctx)
    groups = labels(sorted(data[x].unique())) if x else [y]
    position = {group: index for index, group in enumerate(groups)}
    rng = np.random.default_rng(0)
    option = base(trigger="item")
    option["xAxis"] = index_axis(groups, x or "")
    option["yAxis"] = value_axis(y, scale=True)
    colours = _groups_in_order(data, color) if color else [None]
    for colour in colours:
        part = data if colour is None else data[data[color] == colour]
        xs = [position[g] if x else 0 for g in (part[x] if x else [y] * len(part.index))]
        option["series"].append(
            {
                "type": "scatter",
                "name": colour or y,
                "data": [
                    [num(p + rng.uniform(-0.3, 0.3), 4), num(v)] for p, v in zip(xs, part[y], strict=True)
                ],
                "symbolSize": 6,
                "itemStyle": {"opacity": 0.7},
            }
        )
    option["legend"]["show"] = bool(color)
    return option


def r_ecdf(frame, f, o, ctx):
    import numpy as np

    x = f["x"]
    data, color = d._color_groups(frame, f.get("color"))
    data = data.assign(**{x: d._numeric(data, x)}).dropna(subset=[x])
    option = base()
    option["xAxis"] = value_axis(x, scale=True)
    option["yAxis"] = {
        "type": "value",
        "min": 0,
        "max": 1,
        "name": "Share of values ≤ x",
        "nameLocation": "middle",
        "nameGap": 44,
        "axisLabel": {"formatter": fn("percent", digits=0, scale=100)},
    }
    for label, group in list(data.groupby(color)) if color else [(x, data)]:
        values = np.sort(group[x].to_numpy(dtype=float))
        if len(values) > 2000:
            values = values[np.linspace(0, len(values) - 1, 2000).astype(int)]
        share = np.arange(1, len(values) + 1) / len(values)
        option["series"].append(
            {
                "type": "line",
                "name": str(label),
                "step": "end",
                "showSymbol": False,
                "data": [[num(a), num(b, 4)] for a, b in zip(values, share, strict=True)],
                "lineStyle": {"width": 2.2},
            }
        )
    option["legend"]["show"] = bool(color)
    median = float(data[x].median())
    _insight(
        ctx,
        (
            f"Half of the {x} values are at or below {compact(median)}; read any level on the curve to "
            "find its percentile."
        ),
    )
    return option


def r_ridgeline(frame, f, o, ctx):
    import numpy as np

    data, x, y, _ = d._grouped_axes(frame, f, ctx)
    groups = labels(sorted(data[x].unique()))[:30]
    low, high = float(data[y].min()), float(data[y].max())
    grid = np.linspace(low, high, 160)
    option = base(trigger="item", legend=False)
    option["xAxis"] = value_axis(y, min=num(low), max=num(high))
    option["yAxis"] = {**index_axis(groups), "min": -0.2, "max": len(groups) + 0.6}
    for index, group in enumerate(groups):
        shape = _kde_shape(data[data[x] == group][y].to_numpy(dtype=float), grid) * 1.3
        colour = ctx["colors"][index % len(ctx["colors"])]
        stack = f"ridge-{index}"
        option["series"] += [
            {
                "type": "line",
                "data": [[num(a), index] for a in grid],
                "stack": stack,
                "showSymbol": False,
                "lineStyle": {"opacity": 0},
                "silent": True,
                "z": 100 - index,
                "tooltip": {"show": False},
            },
            {
                "type": "line",
                "name": group,
                "data": [[num(a), num(b, 4)] for a, b in zip(grid, shape, strict=True)],
                "stack": stack,
                "showSymbol": False,
                "smooth": True,
                "z": 100 - index,
                "lineStyle": {"color": colour, "width": 1.5},
                "areaStyle": {"color": rgba(colour, 0.55)},
            },
        ]
    return option


def r_raincloud(frame, f, o, ctx):
    import numpy as np

    data, x, y, _ = d._grouped_axes(frame, f, ctx)
    data = d._sample(data, 4000, ctx)
    groups = labels(sorted(data[x].unique()))[:12] if x else [y]
    low, high = float(data[y].min()), float(data[y].max())
    pad = (high - low) * 0.05 or 1
    grid = np.linspace(low - pad, high + pad, 80)
    rng = np.random.default_rng(0)
    items, points = [], []
    for index, group in enumerate(groups):
        values = (data[data[x] == group] if x else data)[y].to_numpy(dtype=float)
        if not len(values):
            continue
        colour = ctx["colors"][index % len(ctx["colors"])]
        shape = _kde_shape(values, grid) * 0.42
        cloud = [[num(v), num(index + w, 4)] for v, w in zip(grid, shape, strict=True)] + [
            [num(grid[-1]), index],
            [num(grid[0]), index],
        ]
        q1, med, q3 = np.percentile(values, [25, 50, 75])
        shapes = [
            {"type": "polygon", "points": cloud, "color": colour, "opacity": 0.5},
            {
                "type": "polygon",
                "points": [[q1, index - 0.05], [q3, index - 0.05], [q3, index - 0.13], [q1, index - 0.13]],
                "color": colour,
                "opacity": 0.9,
            },
            {
                "type": "polyline",
                "points": [[med, index - 0.03], [med, index - 0.15]],
                "color": ctx["tokens"]["bg"],
                "width": 2,
            },
        ]
        items.append({"name": group, "value": [num(med), index], "shapes": shapes})
        points += [[num(v), num(index - 0.3 + rng.uniform(-0.08, 0.08), 4)] for v in values[:1500]]
    option = base(trigger="item", legend=False)
    option["xAxis"] = value_axis(y, min=num(low - pad), max=num(high + pad))
    option["yAxis"] = {**index_axis(groups), "min": -0.6}
    option["series"] = [
        shapes_series(items, name=y, encode={"x": 0, "y": 1}, tooltip={"formatter": fn("shapeTooltip")}),
        {
            "type": "scatter",
            "name": "Values",
            "data": points,
            "symbolSize": 4,
            "itemStyle": {"color": ctx["tokens"]["muted"], "opacity": 0.55},
        },
    ]
    return option


# --- Statistical -------------------------------------------------------------------------------


def r_mean_ci(frame, f, o, ctx):
    from scipy import stats

    x, y = f["x"], f["y"]
    data = d._labels(frame, [x]).assign(**{y: d._numeric(frame, y)}).dropna(subset=[y])
    level = float(o.get("confidence", "0.95"))
    summary = data.groupby(x)[y].agg(["mean", "std", "count"]).reset_index()
    summary = summary[summary["count"] >= 2].head(MAX_CATEGORIES)
    if summary.empty:
        raise AnalysisError("Each group needs at least two values for a confidence interval.")
    summary["margin"] = [
        stats.t.ppf((1 + level) / 2, n - 1) * sd / math.sqrt(n)
        for sd, n in zip(summary["std"], summary["count"], strict=True)
    ]
    groups = labels(summary[x])
    bars = [
        {
            "value": num(m),
            "itemStyle": {"color": ctx["colors"][i % len(ctx["colors"])], "borderRadius": [4, 4, 0, 0]},
        }
        for i, m in enumerate(summary["mean"])
    ]
    errors = [
        {
            "value": [i, num(m)],
            "shapes": [
                {
                    "type": "polyline",
                    "points": [[i, m - e], [i, m + e]],
                    "color": ctx["tokens"]["fg"],
                    "width": 1.8,
                },
                {
                    "type": "polyline",
                    "points": [[i, m + e, -7, 0], [i, m + e, 7, 0]],
                    "color": ctx["tokens"]["fg"],
                    "width": 1.8,
                },
                {
                    "type": "polyline",
                    "points": [[i, m - e, -7, 0], [i, m - e, 7, 0]],
                    "color": ctx["tokens"]["fg"],
                    "width": 1.8,
                },
            ],
        }
        for i, (m, e) in enumerate(zip(summary["mean"], summary["margin"], strict=True))
    ]
    option = base(legend=False)
    option["tooltip"]["axisPointer"] = {"type": "shadow"}
    option["xAxis"] = category_axis(groups, x)
    option["yAxis"] = value_axis(
        f"Mean {y} ({level:.0%} CI)", min=num(min(0, float((summary["mean"] - summary["margin"]).min())))
    )
    option["series"] = [
        {"type": "bar", "name": f"Mean {y}", "data": bars, "barMaxWidth": 56},
        shapes_series(errors, name="Confidence interval", silent=True, tooltip={"show": False}, z=5),
    ]
    groups_values = [group[y].to_numpy() for _, group in data.groupby(x) if len(group.index) >= 2]
    if len(groups_values) >= 2:
        statistic, p_value = stats.f_oneway(*groups_values[:MAX_CATEGORIES])
        _stat(ctx, "ANOVA F", round(float(statistic), 3))
        _stat(ctx, "ANOVA p-value", float(f"{p_value:.4g}"))
        verdict = "differ significantly" if p_value < 0.05 else "are not significantly different"
        _insight(
            ctx,
            (
                f"The group means {verdict} (ANOVA p = {p_value:.3g}); bars show means and whiskers the "
                f"{level:.0%} confidence interval."
            ),
        )
    return option


def r_qq_plot(frame, f, o, ctx):
    import numpy as np
    from scipy import stats

    distribution = o.get("distribution", "norm")
    values = d._distribution_values(frame, f, distribution)
    dist, params = d._fit(values, distribution)
    if len(values) > 5000:
        values = np.random.default_rng(0).choice(values, 5000, replace=False)
    (theoretical, ordered), (slope, intercept, r) = stats.probplot(values, sparams=params[:-2], dist=dist)
    line = np.array([theoretical.min(), theoretical.max()])
    option = base()
    option["tooltip"]["trigger"] = "item"
    option["xAxis"] = value_axis("Theoretical quantiles", scale=True)
    option["yAxis"] = value_axis(f"Observed {f['x']}", scale=True)
    option["series"] = [
        {
            "type": "scatter",
            "name": "Values",
            "data": [[num(a), num(b)] for a, b in zip(theoretical, ordered, strict=True)],
            "symbolSize": 6,
            "itemStyle": {"opacity": 0.75},
            "large": len(values) > 2000,
        },
        {
            "type": "line",
            "name": "Reference",
            "data": [[num(a), num(slope * a + intercept)] for a in line],
            "showSymbol": False,
            "lineStyle": {"type": "dashed", "color": NEGATIVE, "width": 2},
        },
    ]
    _stat(ctx, "Fit correlation (r)", round(float(r), 4))
    if distribution == "norm" and 3 <= len(values) <= 5000:
        p_value = float(stats.shapiro(values).pvalue)
        _stat(ctx, "Shapiro–Wilk p-value", float(f"{p_value:.4g}"))
        normal = p_value >= 0.05
        follows, looks = ("follow", "looks") if normal else ("bend away from", "is not")
        _insight(
            ctx,
            f"Points {follows} the red line, so the data {looks} normally distributed "
            f"(Shapiro–Wilk p = {p_value:.3g}).",
        )
    else:
        _insight(
            ctx,
            (
                f"The closer the points sit on the red line (r = {r:.3f}), the better the "
                f"{distribution} distribution fits."
            ),
        )
    return option


def r_distribution_fit(frame, f, o, ctx):
    import numpy as np
    from scipy import stats

    distribution = o.get("distribution", "norm")
    values = d._distribution_values(frame, f, distribution)
    dist, params = d._fit(values, distribution)
    edges = _bins(values, int(o.get("bins") or 0) or None)
    counts, _ = np.histogram(values, bins=edges)
    density = counts / max(counts.sum(), 1) / np.diff(edges)
    centres = (edges[:-1] + edges[1:]) / 2
    option = base()
    option["tooltip"]["axisPointer"] = {"type": "shadow"}
    option["xAxis"] = category_axis(_bin_labels(edges), f["x"], axisLabel={"rotate": 35, "hideOverlap": True})
    option["yAxis"] = value_axis("Density")
    option["series"] = [
        {
            "type": "bar",
            "name": "Observed",
            "data": nums(density, 8),
            "barCategoryGap": "4%",
            "itemStyle": {"color": rgba(ctx["colors"][0], 0.6)},
        },
        {
            "type": "line",
            "name": f"Fitted {distribution}",
            "data": nums(dist.pdf(centres, *params), 8),
            "smooth": True,
            "showSymbol": False,
            "lineStyle": {"width": 3, "color": ctx["colors"][2]},
        },
    ]
    statistic, p_value = stats.kstest(values, distribution, args=params)
    _stat(ctx, "KS statistic", round(float(statistic), 4))
    _stat(ctx, "KS p-value", float(f"{p_value:.4g}"))
    for name, value in zip(["shape"] * (len(params) - 2) + ["location", "scale"], params, strict=True):
        _stat(ctx, name.capitalize(), round(float(value), 4))
    _insight(
        ctx,
        (
            f"The fitted {distribution} curve {'matches' if p_value >= 0.05 else 'does not match'} the "
            f"data well (Kolmogorov–Smirnov p = {p_value:.3g})."
        ),
    )
    return option


def r_control_chart(frame, f, o, ctx):
    x, y = f["x"], f["y"]
    data = frame[[x, y]].copy()
    data[y] = d._numeric(data, y)
    data = data.dropna(subset=[y])
    is_date = ctx["kinds"].get(x) == "datetime"
    if is_date:
        data[x] = d._dates(data[x])
    data = d._sample(data.sort_values(x, kind="mergesort"), 5000, ctx).sort_values(x, kind="mergesort")
    mean, sd = float(data[y].mean()), float(data[y].std() or 0)
    sigma = float(o.get("sigma", 3))
    upper, lower = mean + sigma * sd, mean - sigma * sd
    outside = (data[y] > upper) | (data[y] < lower)
    xs = dates(data[x]) if is_date else labels(data[x])
    option = base(legend=False, zoom=len(data.index) > 80)
    option["xAxis"] = time_axis(x) if is_date else category_axis(xs, x)
    option["yAxis"] = value_axis(y, scale=True)
    points = [
        {
            "value": [s, num(v)] if is_date else num(v),
            "itemStyle": {"color": NEGATIVE if bad else ctx["colors"][0]},
            "symbolSize": 9 if bad else 5,
        }
        for s, v, bad in zip(xs, data[y], outside, strict=True)
    ]
    option["series"] = [
        {
            "type": "line",
            "name": y,
            "data": points,
            "showSymbol": True,
            "lineStyle": {"width": 1.6},
            "markLine": {
                "silent": True,
                "symbol": "none",
                "data": [
                    {
                        "yAxis": num(mean),
                        "name": "Mean",
                        "lineStyle": {"color": ctx["tokens"]["muted"], "type": "solid"},
                    },
                    {
                        "yAxis": num(upper),
                        "name": f"UCL +{sigma:g}σ",
                        "lineStyle": {"color": NEGATIVE, "type": "dashed"},
                    },
                    {
                        "yAxis": num(lower),
                        "name": f"LCL −{sigma:g}σ",
                        "lineStyle": {"color": NEGATIVE, "type": "dashed"},
                    },
                ],
                "label": {"formatter": "{b}", "position": "insideEndTop"},
            },
        }
    ]
    _stat(ctx, "Out of control", int(outside.sum()), "integer")
    _stat(ctx, "Mean", round(mean, 4))
    _stat(ctx, "Std. deviation", round(sd, 4))
    state = "stable" if not outside.any() else "not fully in control"
    _insight(
        ctx,
        f"{int(outside.sum())} measurement(s) fall outside the ±{sigma:g}σ control limits (shown in red); "
        f"the process is {state}.",
    )
    return option


def r_bland_altman(frame, f, o, ctx):
    data = d._require_numeric(frame, [f["x"], f["y"]], 3)
    average = (data[f["x"]] + data[f["y"]]) / 2
    difference = data[f["x"]] - data[f["y"]]
    bias, sd = float(difference.mean()), float(difference.std())
    option = base(legend=False)
    option["tooltip"]["trigger"] = "item"
    option["xAxis"] = value_axis(f"Mean of {f['x']} and {f['y']}", scale=True)
    option["yAxis"] = value_axis(f"{f['x']} − {f['y']}", scale=True)
    option["series"] = [
        {
            "type": "scatter",
            "name": "Pairs",
            "data": [[num(a), num(b)] for a, b in zip(average, difference, strict=True)],
            "symbolSize": 6,
            "itemStyle": {"opacity": 0.7},
            "markLine": {
                "silent": True,
                "symbol": "none",
                "data": [
                    {
                        "yAxis": num(bias),
                        "name": f"Bias {bias:.3g}",
                        "lineStyle": {"color": ctx["colors"][2]},
                    },
                    {
                        "yAxis": num(bias + 1.96 * sd),
                        "name": "+1.96 SD",
                        "lineStyle": {"color": NEGATIVE, "type": "dashed"},
                    },
                    {
                        "yAxis": num(bias - 1.96 * sd),
                        "name": "−1.96 SD",
                        "lineStyle": {"color": NEGATIVE, "type": "dashed"},
                    },
                ],
                "label": {"formatter": "{b}", "position": "insideEndTop"},
            },
        }
    ]
    _stat(ctx, "Bias", round(bias, 4))
    _insight(
        ctx,
        (
            f"On average {f['x']} reads {compact(bias)} {'higher' if bias >= 0 else 'lower'} than "
            f"{f['y']}; 95% of differences lie within ±{compact(1.96 * sd)}."
        ),
    )
    return option


def r_missing_values(frame, f, o, ctx):
    columns = (f.get("features") or list(frame.columns))[:60]
    nulls = frame[columns].isna()
    share = (nulls.mean() * 100).sort_values(ascending=False)
    sample = d._sample(nulls, 300, ctx, "pattern").astype(int)
    ordered = labels(share.index)
    option = base(trigger="item", legend=False)
    option["grid"] = [
        {"left": 50, "right": 20, "top": 36, "height": "30%"},
        {"left": 50, "right": 20, "top": "52%", "bottom": 30},
    ]
    option["title"] = [
        {"text": "Missing share per column", "left": 8, "top": 4},
        {"text": "Missing pattern (sample rows × columns)", "left": 8, "top": "45%"},
    ]
    option["xAxis"] = [
        category_axis(ordered, gridIndex=0, axisLabel={"rotate": 35, "hideOverlap": True}),
        category_axis(ordered, gridIndex=1, axisLabel={"show": False}),
    ]
    option["yAxis"] = [
        {"type": "value", "gridIndex": 0, "max": 100, "axisLabel": {"formatter": "{value}%"}},
        {
            "type": "category",
            "gridIndex": 1,
            "data": [str(i) for i in range(len(sample.index))],
            "axisLabel": {"show": False},
            "inverse": True,
        },
    ]
    cells = [
        [j, i, int(sample[column].iat[i])]
        for j, column in enumerate(ordered)
        for i in range(len(sample.index))
        if sample[column].iat[i]
    ]
    option["visualMap"] = {
        "show": False,
        "type": "piecewise",
        "seriesIndex": 1,
        "pieces": [{"value": 1, "color": NEGATIVE}],
    }
    option["series"] = [
        {
            "type": "bar",
            "xAxisIndex": 0,
            "yAxisIndex": 0,
            "data": [
                {"value": num(v, 2), "itemStyle": {"color": NEGATIVE if v > 20 else ctx["colors"][0]}}
                for v in share.values
            ],
            "tooltip": {"valueFormatter": fn("percent", digits=1)},
        },
        {"type": "heatmap", "xAxisIndex": 1, "yAxisIndex": 1, "data": cells, "itemStyle": {"borderWidth": 0}},
    ]
    complete = float((~nulls.any(axis=1)).mean())
    _stat(ctx, "Complete rows", f"{complete:.1%}", "text")
    _insight(
        ctx,
        f"{complete:.0%} of rows are complete; {ordered[0]} has the most gaps ({share.iloc[0]:.1f}% missing)."
        if len(share) and share.iloc[0] > 0
        else "No values are missing.",
    )
    return option


# --- Relationship ------------------------------------------------------------------------------


def _size_scale(values: Any) -> list[float]:
    import numpy as np

    array = np.asarray(values, dtype=float)
    array = np.nan_to_num(np.abs(array))
    top = array.max() if len(array) and array.max() > 0 else 1
    return [round(6 + 34 * math.sqrt(v / top), 1) for v in array]


def _scatter_series(
    data: Any, x: str, y: str, color: str | None, size: str | None, ctx: dict[str, Any], opacity: float = 0.8
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Scatter series grouped by a categorical colour, or one series with a numeric colour scale."""
    large = len(data.index) > 2000 and not size
    if color and ctx["kinds"].get(color) == "numeric":
        values = d._numeric(data, color)
        sizes = _size_scale(data[size]) if size else None
        points = [
            {"value": [num(a), num(b), num(c)], **({"symbolSize": sizes[i]} if sizes else {})}
            for i, (a, b, c) in enumerate(zip(data[x], data[y], values, strict=True))
        ]
        visual = {
            "dimension": 2,
            "min": num(values.min()),
            "max": num(values.max()),
            "calculable": True,
            "orient": "vertical",
            "right": 0,
            "top": "middle",
            "text": [color, ""],
            "inRange": {"color": ctx["seq"]},
        }
        return [
            {
                "type": "scatter",
                "name": y,
                "data": points,
                "symbolSize": 8,
                "large": large,
                "itemStyle": {"opacity": opacity},
            }
        ], visual
    groups = (
        [(None, data)]
        if not color
        else [(group, data[data[color] == group]) for group in _groups_in_order(data, color)]
    )
    series = []
    for group, part in groups:
        sizes = _size_scale(part[size]) if size else None
        points = (
            [
                {"value": [num(a), num(b)], "symbolSize": sizes[i]}
                for i, (a, b) in enumerate(zip(part[x], part[y], strict=True))
            ]
            if sizes
            else [[num(a), num(b)] for a, b in zip(part[x], part[y], strict=True)]
        )
        series.append(
            {
                "type": "scatter",
                "name": group or y,
                "data": points,
                "symbolSize": 8,
                "large": large,
                "itemStyle": {"opacity": opacity, "borderColor": ctx["tokens"]["bg"], "borderWidth": 0.5},
                "emphasis": {"focus": "series"},
            }
        )
    return series, None


def _trend_series(data: Any, x: str, y: str, kind: str, ctx: dict[str, Any]) -> dict[str, Any] | None:
    import numpy as np

    clean = data[[x, y]].dropna()
    if len(clean.index) < 3 or kind == "none":
        return None
    if kind == "lowess":
        from statsmodels.nonparametric.smoothers_lowess import lowess

        fitted = lowess(clean[y], clean[x], frac=0.3, return_sorted=True)
        points = [[num(a), num(b)] for a, b in fitted[:: max(1, len(fitted) // 300)]]
        name = "LOWESS trend"
    else:
        slope, intercept = np.polyfit(clean[x], clean[y], 1)
        xs = [float(clean[x].min()), float(clean[x].max())]
        points = [[num(a), num(slope * a + intercept)] for a in xs]
        name = f"Linear trend (slope {slope:.3g})"
    return {
        "type": "line",
        "name": name,
        "data": points,
        "showSymbol": False,
        "smooth": kind == "lowess",
        "lineStyle": {"width": 2.6, "type": "solid", "color": ctx["tokens"]["fg"]},
        "z": 10,
    }


def _correlation_insight(ctx, data, x, y):
    clean = data[[x, y]].dropna()
    if len(clean.index) >= 3 and clean[x].std() and clean[y].std():
        r = float(clean[x].corr(clean[y]))
        strength = "strong" if abs(r) >= 0.7 else "moderate" if abs(r) >= 0.4 else "weak"
        direction = "rises" if r > 0 else "falls"
        _insight(
            ctx,
            (
                f"There is a {strength} {'positive' if r > 0 else 'negative'} relationship (r = "
                f"{r:.2f}): as {x} increases, {y} generally {direction}."
            )
            if abs(r) >= 0.2
            else f"{x} and {y} show little linear relationship (r = {r:.2f}).",
        )


def _marginals(option: dict[str, Any], data: Any, x: str, y: str, ctx: dict[str, Any], kind: str) -> None:
    """Histograms (or box plots) of each axis above and to the right of a scatter."""
    import numpy as np

    option["grid"] = [
        {"left": 50, "right": 130, "top": 120, "bottom": 40},
        {"left": 50, "right": 130, "top": 36, "height": 70},
        {"right": 20, "width": 100, "top": 120, "bottom": 40},
    ]
    main_x, main_y = option["xAxis"], option["yAxis"]
    main_x.update({"gridIndex": 0})
    main_y.update({"gridIndex": 0})
    option["xAxis"] = [
        main_x,
        {**value_axis("", gridIndex=1, scale=True), "axisLabel": {"show": False}},
        {"type": "value", "gridIndex": 2, "axisLabel": {"show": False}, "splitLine": {"show": False}},
    ]
    option["yAxis"] = [
        main_y,
        {"type": "value", "gridIndex": 1, "axisLabel": {"show": False}, "splitLine": {"show": False}},
        {**value_axis("", gridIndex=2, scale=True), "axisLabel": {"show": False}},
    ]
    colour = rgba(ctx["colors"][0], 0.55)
    if kind == "box":
        for axis_index, column, horizontal in ((1, x, True), (2, y, False)):
            box, _ = _five(data[column].dropna().to_numpy(dtype=float))
            option["series"].append(
                {
                    "type": "custom",
                    "renderItem": fn("shapes"),
                    "xAxisIndex": axis_index,
                    "yAxisIndex": axis_index,
                    "silent": True,
                    "data": [
                        {
                            "value": [box[2], 0.5] if horizontal else [0.5, box[2]],
                            "shapes": [
                                {
                                    "type": "polygon",
                                    "points": [[box[1], 0.3], [box[3], 0.3], [box[3], 0.7], [box[1], 0.7]]
                                    if horizontal
                                    else [[0.3, box[1]], [0.7, box[1]], [0.7, box[3]], [0.3, box[3]]],
                                    "color": colour,
                                },
                                {
                                    "type": "polyline",
                                    "points": [[box[0], 0.5], [box[4], 0.5]]
                                    if horizontal
                                    else [[0.5, box[0]], [0.5, box[4]]],
                                    "color": ctx["colors"][0],
                                    "width": 1.5,
                                },
                            ],
                        }
                    ],
                }
            )
        option["yAxis"][1].update({"min": 0, "max": 1})
        option["xAxis"][2].update({"min": 0, "max": 1})
        return
    for axis_index, column, horizontal in ((1, x, True), (2, y, False)):
        values = data[column].dropna().to_numpy(dtype=float)
        edges = _bins(values, 30)
        counts, _ = np.histogram(values, bins=edges)
        items = [
            {
                "value": [num((a + b) / 2), int(c)] if horizontal else [int(c), num((a + b) / 2)],
                "shapes": [
                    {
                        "type": "polygon",
                        "points": [[a, 0], [b, 0], [b, c], [a, c]]
                        if horizontal
                        else [[0, a], [0, b], [c, b], [c, a]],
                        "color": colour,
                    }
                ],
            }
            for a, b, c in zip(edges[:-1], edges[1:], counts, strict=True)
        ]
        option["series"].append(
            {
                "type": "custom",
                "renderItem": fn("shapes"),
                "xAxisIndex": axis_index,
                "yAxisIndex": axis_index,
                "data": items,
                "tooltip": {"show": False},
                "silent": True,
            }
        )
        if horizontal:
            option["yAxis"][1]["max"] = int(counts.max()) or 1
        else:
            option["xAxis"][2]["max"] = int(counts.max()) or 1


def r_scatter(frame, f, o, ctx):
    data = d._xy(frame, f, ctx, ("size",))
    color = f.get("color")
    if color and ctx["kinds"].get(color) != "numeric":
        data = _limit_series(data, color, ctx)
    option = base(trigger="item")
    option["xAxis"] = value_axis(f["x"], scale=True)
    option["yAxis"] = value_axis(f["y"], scale=True)
    series, visual = _scatter_series(
        data, f["x"], f["y"], color, f.get("size"), ctx, float(o.get("opacity", 0.8))
    )
    option["series"] = series
    if visual:
        option["visualMap"] = visual
        option["grid"]["right"] = 80
    trend = _trend_series(data, f["x"], f["y"], o.get("trendline", "none"), ctx)
    if trend:
        option["series"].append(trend)
    option["legend"]["show"] = bool(color) and not visual or bool(trend)
    if o.get("marginal") in {"histogram", "box"}:
        _marginals(option, data, f["x"], f["y"], ctx, o["marginal"])
    d._correlation_stats(ctx, data[f["x"]], data[f["y"]])
    _correlation_insight(ctx, data, f["x"], f["y"])
    return option


def r_bubble(frame, f, o, ctx):
    return r_scatter(frame, f, {**o, "trendline": "none", "marginal": ""}, ctx)


def r_regression_fit(frame, f, o, ctx):
    import numpy as np
    import statsmodels.api as sm

    data = d._xy(frame, f, ctx)
    x, y = data[f["x"]].to_numpy(dtype=float), data[f["y"]].to_numpy(dtype=float)
    degree = int(o.get("degree", 1))
    if len(x) <= degree + 1:
        raise AnalysisError("Not enough rows for this polynomial degree.")
    model = sm.OLS(y, np.vander(x, degree + 1, increasing=True)).fit()
    grid = np.linspace(x.min(), x.max(), 160)
    prediction = model.get_prediction(np.vander(grid, degree + 1, increasing=True)).summary_frame(alpha=0.05)
    colour = ctx["colors"][2]
    option = base(trigger="item")
    option["xAxis"] = value_axis(f["x"], scale=True)
    option["yAxis"] = value_axis(f["y"], scale=True)
    option["series"] = [
        {
            "type": "scatter",
            "name": "Observations",
            "data": [[num(a), num(b)] for a, b in zip(x, y, strict=True)],
            "symbolSize": 7,
            "itemStyle": {"opacity": 0.65, "color": ctx["colors"][0]},
            "large": len(x) > 2000,
        }
    ]
    if o.get("band", True):
        option["series"] += [
            {
                "type": "line",
                "name": "lower",
                "data": [[num(a), num(b)] for a, b in zip(grid, prediction["mean_ci_lower"], strict=True)],
                "stack": "ci",
                "showSymbol": False,
                "lineStyle": {"opacity": 0},
                "tooltip": {"show": False},
                "silent": True,
            },
            {
                "type": "line",
                "name": "95% confidence",
                "data": [
                    [num(a), num(b)]
                    for a, b in zip(
                        grid, prediction["mean_ci_upper"] - prediction["mean_ci_lower"], strict=True
                    )
                ],
                "stack": "ci",
                "showSymbol": False,
                "lineStyle": {"opacity": 0},
                "areaStyle": {"color": rgba(colour, 0.22)},
                "tooltip": {"show": False},
                "silent": True,
            },
        ]
    option["series"].append(
        {
            "type": "line",
            "name": "Fit",
            "data": [[num(a), num(b)] for a, b in zip(grid, prediction["mean"], strict=True)],
            "showSymbol": False,
            "smooth": True,
            "lineStyle": {"width": 3, "color": colour},
            "itemStyle": {"color": colour},
        }
    )
    option["legend"]["data"] = ["Observations", "Fit"] + (["95% confidence"] if o.get("band", True) else [])
    terms = " + ".join(
        f"{coef:.4g}·x^{power}" if power > 1 else (f"{coef:.4g}·x" if power == 1 else f"{coef:.4g}")
        for power, coef in enumerate(model.params)
    )
    option["graphic"] = [
        {
            "type": "text",
            "left": 70,
            "top": 40,
            "style": {
                "text": f"y = {terms}\nR² = {model.rsquared:.4f}",
                "fill": ctx["tokens"]["fg"],
                "font": "12px sans-serif",
                "backgroundColor": rgba(ctx["tokens"]["bg"], 0.85),
                "padding": 6,
            },
        }
    ]
    _stat(ctx, "R²", round(float(model.rsquared), 4))
    _stat(ctx, "Adjusted R²", round(float(model.rsquared_adj), 4))
    _stat(ctx, "Model p-value", float(f"{model.f_pvalue:.4g}"))
    _insight(
        ctx,
        (
            f"The fitted curve explains {max(model.rsquared, 0):.0%} of the variation in {f['y']} (R² = "
            f"{model.rsquared:.3f}); the band is the 95% confidence interval of the fit."
        ),
    )
    return option


def r_correlation_heatmap(frame, f, o, ctx):
    import numpy as np

    columns = d._feature_columns(frame, f, ctx["kinds"])[:40]
    data = frame[columns].apply(lambda s: d._numeric(frame, s.name))
    matrix = data.corr(method=o.get("method", "pearson"))
    triangle = o.get("triangle", True)
    cells = [
        [j, i, num(matrix.iat[i, j], 2)]
        for i in range(len(columns))
        for j in range(len(columns))
        if not triangle or j <= i
    ]
    option = _heatmap(
        columns,
        columns,
        cells,
        ctx,
        diverging=True,
        minimum=-1,
        maximum=1,
        show_values=bool(o.get("show_values", True)),
        name="Correlation",
    )
    pairs = matrix.where(~np.eye(len(matrix), dtype=bool)).stack()
    if not pairs.empty:
        strongest = pairs.abs().idxmax()
        _stat(ctx, "Strongest pair", f"{strongest[0]} ↔ {strongest[1]} ({pairs[strongest]:.2f})", "text")
        _insight(
            ctx,
            (
                f"The strongest relationship is {strongest[0]} ↔ {strongest[1]} "
                f"({pairs[strongest]:+.2f}); red is positive, blue negative."
            ),
        )
    return option


def r_scatter_matrix(frame, f, o, ctx):
    import numpy as np

    color = f.get("color")
    columns = d._feature_columns(frame, f, ctx["kinds"], tuple(filter(None, [color])))[:6]
    data = frame[columns + ([color] if color else [])].copy()
    for column in columns:
        data[column] = d._numeric(data, column)
    data = d._sample(data.dropna(subset=columns), 1500, ctx)
    if color:
        data = _limit_series(data, color, ctx)
    n = len(columns)
    grids = panels(n * n, n, top=6, gap=2.5)
    option = base(trigger="item")
    option["grid"], option["xAxis"], option["yAxis"] = grids, [], []
    groups = _groups_in_order(data, color) if color else [None]
    for row in range(n):
        for col in range(n):
            index = row * n + col
            option["xAxis"].append(
                {
                    "type": "value",
                    "gridIndex": index,
                    "scale": True,
                    "name": columns[col] if row == n - 1 else "",
                    "nameLocation": "middle",
                    "nameGap": 22,
                    "axisLabel": {"show": False},
                    "splitLine": {"show": False},
                }
            )
            option["yAxis"].append(
                {
                    "type": "value",
                    "gridIndex": index,
                    "scale": True,
                    "name": columns[row] if col == 0 else "",
                    "nameLocation": "middle",
                    "nameGap": 12,
                    "axisLabel": {"show": False},
                    "splitLine": {"show": False},
                }
            )
            if row == col:
                values = data[columns[col]].to_numpy(dtype=float)
                edges = _bins(values, 20)
                counts, _ = np.histogram(values, bins=edges)
                option["yAxis"][-1].update({"min": 0, "max": int(counts.max()) or 1, "scale": False})
                option["series"].append(
                    {
                        "type": "custom",
                        "renderItem": fn("shapes"),
                        "xAxisIndex": index,
                        "yAxisIndex": index,
                        "silent": True,
                        "tooltip": {"show": False},
                        "data": [
                            {
                                "value": [num((a + b) / 2), int(c)],
                                "shapes": [
                                    {
                                        "type": "polygon",
                                        "points": [[a, 0], [b, 0], [b, c], [a, c]],
                                        "color": rgba(ctx["colors"][0], 0.6),
                                    }
                                ],
                            }
                            for a, b, c in zip(edges[:-1], edges[1:], counts, strict=True)
                        ],
                    }
                )
            elif row > col:
                for group in groups:
                    part = data if group is None else data[data[color] == group]
                    option["series"].append(
                        {
                            "type": "scatter",
                            "name": group or "Rows",
                            "xAxisIndex": index,
                            "yAxisIndex": index,
                            "data": [
                                [num(a), num(b)]
                                for a, b in zip(part[columns[col]], part[columns[row]], strict=True)
                            ],
                            "symbolSize": 4,
                            "itemStyle": {"opacity": 0.6},
                        }
                    )
            else:
                r = float(data[columns[col]].corr(data[columns[row]]))
                option["series"].append(
                    {"type": "scatter", "xAxisIndex": index, "yAxisIndex": index, "data": [], "silent": True}
                )
                option.setdefault("graphic", []).append(
                    {
                        "type": "text",
                        "left": f"{_pct(grids[index]['left']) + _pct(grids[index]['width']) / 2 - 2:.1f}%",
                        "top": f"{_pct(grids[index]['top']) + _pct(grids[index]['height']) / 2 - 2:.1f}%",
                        "style": {
                            "text": f"r = {r:.2f}",
                            "fill": NEGATIVE
                            if r > 0.5
                            else ("#2166AC" if r < -0.5 else ctx["tokens"]["muted"]),
                            "font": "600 13px sans-serif",
                        },
                    }
                )
    option["legend"]["show"] = bool(color)
    if bool(color):
        option["legend"]["data"] = groups
    return option


def r_density_heatmap(frame, f, o, ctx):
    import numpy as np

    data = d._xy(frame, f, ctx)
    bins = int(o.get("nbins", 40))
    counts, xedges, yedges = np.histogram2d(data[f["x"]], data[f["y"]], bins=bins)
    xs = [compact(float((a + b) / 2)) for a, b in zip(xedges[:-1], xedges[1:], strict=True)]
    ys = [compact(float((a + b) / 2)) for a, b in zip(yedges[:-1], yedges[1:], strict=True)]
    cells = [[i, j, int(counts[i, j])] for i in range(len(xs)) for j in range(len(ys)) if counts[i, j]]
    option = _heatmap(xs, ys, cells, ctx, show_values=False, name="Rows")
    option["yAxis"]["inverse"] = False
    option["xAxis"]["name"], option["yAxis"]["name"] = f["x"], f["y"]
    peak = np.unravel_index(counts.argmax(), counts.shape)
    _insight(ctx, f"Rows concentrate around {f['x']} ≈ {xs[peak[0]]} and {f['y']} ≈ {ys[peak[1]]}.")
    return option


def r_density_contour(frame, f, o, ctx):
    import contourpy
    import numpy as np
    from scipy.stats import gaussian_kde

    data = d._xy(frame, f, ctx)
    sample = d._sample(data, 3000, {"notes": [], "sampled": False})
    x, y = sample[f["x"]].to_numpy(dtype=float), sample[f["y"]].to_numpy(dtype=float)
    if np.ptp(x) == 0 or np.ptp(y) == 0:
        raise AnalysisError("Both columns need varying values for a density contour.")
    xs, ys = np.linspace(x.min(), x.max(), 70), np.linspace(y.min(), y.max(), 70)
    mesh_x, mesh_y = np.meshgrid(xs, ys)
    density = gaussian_kde(np.vstack([x, y]))(np.vstack([mesh_x.ravel(), mesh_y.ravel()])).reshape(
        mesh_x.shape
    )
    generator = contourpy.contour_generator(mesh_x, mesh_y, density)
    levels = np.linspace(density.max() * 0.08, density.max() * 0.92, 7)
    option = base(trigger="item", legend=False)
    option["xAxis"] = value_axis(f["x"], scale=True)
    option["yAxis"] = value_axis(f["y"], scale=True)
    option["series"] = [
        {
            "type": "scatter",
            "name": "Rows",
            "data": [[num(a), num(b)] for a, b in zip(x, y, strict=True)],
            "symbolSize": 3,
            "itemStyle": {"color": ctx["tokens"]["muted"], "opacity": 0.35},
            "large": True,
        }
    ]
    palette = ctx["seq"]
    for index, level in enumerate(levels):
        points: list[Any] = []
        for line in generator.lines(level):
            points += [[num(a), num(b)] for a, b in line] + [None]
        colour = palette[min(len(palette) - 1, int(index / max(len(levels) - 1, 1) * (len(palette) - 1)))]
        option["series"].append(
            {
                "type": "line",
                "name": f"Density level {index + 1}",
                "data": points,
                "showSymbol": False,
                "connectNulls": False,
                "lineStyle": {"width": 1.4 + index * 0.25, "color": colour},
                "tooltip": {"show": False},
                "silent": True,
            }
        )
    peak = np.unravel_index(density.argmax(), density.shape)
    _insight(
        ctx,
        (
            f"The densest area is around {f['x']} ≈ {compact(float(xs[peak[1]]))}, {f['y']} ≈ "
            f"{compact(float(ys[peak[0]]))}; inner rings hold more rows."
        ),
    )
    return option


def r_joint_plot(frame, f, o, ctx):
    return r_scatter(frame, f, {**o, "marginal": "histogram"}, ctx)


def r_parallel_coordinates(frame, f, o, ctx):
    columns = d._feature_columns(frame, f, ctx["kinds"])[:12]
    color = f.get("color") or columns[0]
    data = frame[list(dict.fromkeys(columns + [color]))].apply(lambda s: d._numeric(frame, s.name)).dropna()
    data = d._sample(data, 3000, ctx, "lines")
    option = base(trigger="item", legend=False)
    option.pop("grid")
    option["parallel"] = {"left": 40, "right": 90, "top": 40, "bottom": 30}
    option["parallelAxis"] = [
        {"dim": i, "name": column, "scale": True, "nameTextStyle": {"fontWeight": 600}}
        for i, column in enumerate(columns)
    ]
    option["visualMap"] = {
        "dimension": columns.index(color) if color in columns else len(columns),
        "min": num(data[color].min()),
        "max": num(data[color].max()),
        "calculable": True,
        "orient": "vertical",
        "right": 0,
        "top": "middle",
        "text": [color, ""],
        "inRange": {"color": ctx["seq"]},
    }
    rows = data[columns + ([color] if color not in columns else [])]
    option["series"] = [
        {
            "type": "parallel",
            "lineStyle": {"width": 1, "opacity": 0.35},
            "data": [nums(row, 6) for row in rows.to_numpy()],
        }
    ]
    return option


def _unique_names(level: int, name: str) -> str:
    return f"{name}{chr(0x200B) * level}"  # zero-width suffix keeps labels identical across levels


def _sankey(
    frame,
    path: list[str],
    value_column: str | None,
    how: str,
    ctx: dict[str, Any],
    limit_values: bool = False,
) -> dict[str, Any]:
    data = d._labels(frame, path)
    if limit_values:
        for column in path:
            top = data[column].value_counts().head(12).index
            data[column] = data[column].where(data[column].isin(top), "Other")
    data, value = d._aggregate(data, path, value_column, how)
    data = data[data[value] > 0]
    nodes: dict[str, int] = {}
    links = []
    for level in range(len(path) - 1):
        pairs = data.groupby([path[level], path[level + 1]])[value].sum().reset_index().nlargest(200, value)
        for _, row in pairs.iterrows():
            source, target = (
                _unique_names(level, str(row[path[level]])),
                _unique_names(level + 1, str(row[path[level + 1]])),
            )
            nodes.setdefault(source, len(nodes))
            nodes.setdefault(target, len(nodes))
            links.append({"source": source, "target": target, "value": num(row[value])})
    option = base(trigger="item", legend=False)
    option.pop("grid")
    option["series"] = [
        {
            "type": "sankey",
            "left": 20,
            "right": 110,
            "top": 30,
            "bottom": 20,
            "emphasis": {"focus": "adjacency"},
            "nodeGap": 10,
            "lineStyle": {"color": "gradient", "opacity": 0.4, "curveness": 0.5},
            "label": {"color": ctx["tokens"]["fg"]},
            "data": [{"name": name} for name in nodes],
            "links": links,
        }
    ]
    if links:
        biggest = max(links, key=lambda link: link["value"] or 0)
        source, target = biggest["source"].strip(chr(0x200B)), biggest["target"].strip(chr(0x200B))
        _insight(ctx, f"The largest flow is {source} → {target} ({compact(biggest['value'] or 0)}).")
    return option


def r_parallel_categories(frame, f, o, ctx):
    path = f["path"][:6]
    if len(path) < 2:
        raise AnalysisError("Choose at least two categorical columns.")
    return _sankey(frame, path, None, "count", ctx, limit_values=True)


def r_crosstab_heatmap(frame, f, o, ctx):
    import pandas as pd
    from scipy import stats

    x, y = f["x"], f["y"]
    data = d._labels(frame, [x, y])
    for column in (x, y):
        data = data[data[column].isin(data[column].value_counts().head(30).index)]
    counts = pd.crosstab(data[y], data[x])
    normalize = o.get("normalize", "none")
    shown = counts if normalize == "none" else pd.crosstab(data[y], data[x], normalize=normalize) * 100
    cells = [[j, i, num(shown.iat[i, j], 1)] for i in range(shown.shape[0]) for j in range(shown.shape[1])]
    option = _heatmap(
        labels(shown.columns), labels(shown.index), cells, ctx, name="Count" if normalize == "none" else "%"
    )
    option["xAxis"]["name"], option["yAxis"]["name"] = x, y
    if counts.shape[0] > 1 and counts.shape[1] > 1:
        chi2, p_value, _, _ = stats.chi2_contingency(counts)
        cramers = math.sqrt(chi2 / (counts.values.sum() * (min(counts.shape) - 1)))
        _stat(ctx, "χ²", round(float(chi2), 3))
        _stat(ctx, "p-value", float(f"{p_value:.4g}"))
        _stat(ctx, "Cramér's V", round(cramers, 4))
        _insight(
            ctx,
            (
                f"{x} and {y} are {'associated' if p_value < 0.05 else 'not significantly associated'} "
                f"(χ² p = {p_value:.3g}, Cramér's V = {cramers:.2f})."
            ),
        )
    return option


# --- Flow --------------------------------------------------------------------------------------


def r_sankey(frame, f, o, ctx):
    path = f["path"][:6]
    if len(path) < 2:
        raise AnalysisError("A Sankey diagram needs at least two stage columns.")
    return _sankey(frame, path, f.get("y"), o.get("aggregation", "sum"), ctx)


def r_network(frame, f, o, ctx):
    import numpy as np

    source, target, weight = f["source"], f["target"], f.get("y")
    data, value = d._aggregate(frame, [source, target], weight, "sum")
    data = data.nlargest(600, value)
    names = sorted(set(data[source]) | set(data[target]))[:300]
    data = data[data[source].isin(names) & data[target].isin(names)]
    index = {name: i for i, name in enumerate(names)}
    count = len(names)
    rng = np.random.default_rng(0)
    position = rng.uniform(-1, 1, (count, 2))
    edges = (
        np.array([[index[a], index[b]] for a, b in zip(data[source], data[target], strict=True)])
        if len(data.index)
        else np.zeros((0, 2), int)
    )
    k = 1 / math.sqrt(max(count, 1))
    for step in range(120):  # Fruchterman–Reingold, deterministic seed
        delta = position[:, None, :] - position[None, :, :]
        distance = np.maximum(np.linalg.norm(delta, axis=2), 1e-3)
        displacement = ((delta / distance[..., None]) * (k * k / distance)[..., None]).sum(axis=1)
        if len(edges):
            vector = position[edges[:, 0]] - position[edges[:, 1]]
            pull = vector * (np.maximum(np.linalg.norm(vector, axis=1), 1e-3) / k)[:, None]
            np.add.at(displacement, edges[:, 0], -pull)
            np.add.at(displacement, edges[:, 1], pull)
        size = np.maximum(np.linalg.norm(displacement, axis=1), 1e-9)
        position += displacement / size[:, None] * np.minimum(size, 0.1 * (1 - step / 120) + 0.005)[:, None]
    degree = np.bincount(edges.ravel(), minlength=count) if len(edges) else np.zeros(count)
    top = max(int(degree.max()) if count else 1, 1)
    palette = ctx["seq"]
    nodes = [
        {
            "name": name,
            "x": num(position[i, 0] * 400, 2),
            "y": num(position[i, 1] * 300, 2),
            "value": int(degree[i]),
            "symbolSize": round(8 + 24 * degree[i] / top, 1),
            "itemStyle": {"color": palette[min(len(palette) - 1, int(degree[i] / top * (len(palette) - 1)))]},
        }
        for i, name in enumerate(names)
    ]
    option = base(trigger="item", legend=False)
    option.pop("grid")
    option["series"] = [
        {
            "type": "graph",
            "layout": "none",
            "roam": True,
            "data": nodes,
            "links": [
                {"source": a, "target": b, "value": num(v)}
                for a, b, v in zip(data[source], data[target], data[value], strict=True)
            ],
            "label": {"show": count <= 60, "position": "right", "color": ctx["tokens"]["fg"]},
            "lineStyle": {"color": ctx["tokens"]["line"], "opacity": 0.7, "width": 1},
            "emphasis": {"focus": "adjacency", "lineStyle": {"width": 3}},
        }
    ]
    _stat(ctx, "Nodes", count, "integer")
    _stat(ctx, "Links", int(len(edges)), "integer")
    if count:
        hub = names[int(degree.argmax())]
        _insight(
            ctx,
            (
                f"{hub} is the most connected node ({int(degree.max())} links); bigger, darker nodes "
                "have more connections."
            ),
        )
    return option


# --- Maps --------------------------------------------------------------------------------------


_WORLD: dict[str, str] | None = None


def world_names() -> dict[str, str]:
    """Lower-cased country names, admin names and ISO-3 codes → the map's feature name."""
    global _WORLD
    if _WORLD is None:
        import json

        features = json.loads(WORLD_PATH.read_text())["features"]
        _WORLD = {}
        for feature in features:
            props = feature["properties"]
            for key in (props["name"], props["admin"], props["iso3"]):
                if key:
                    _WORLD[str(key).lower()] = props["name"]
        aliases = {
            "usa": "united states of america",
            "us": "united states of america",
            "united states": "united states of america",
            "uk": "united kingdom",
            "great britain": "united kingdom",
            "drc": "dem. rep. congo",
            "russia": "russia",
            "south korea": "south korea",
            "czech republic": "czechia",
            "ivory coast": "côte d'ivoire",
        }
        for alias, target in aliases.items():
            if target in _WORLD:
                _WORLD.setdefault(alias, _WORLD[target])
    return _WORLD


_CONTINENTS: dict[str, str] | None = None


def world_continents() -> dict[str, str]:
    """The map's feature name → continent (from the vendored Natural Earth file)."""
    global _CONTINENTS
    if _CONTINENTS is None:
        import json

        features = json.loads(WORLD_PATH.read_text())["features"]
        _CONTINENTS = {f["properties"]["name"]: f["properties"].get("continent", "") for f in features}
    return _CONTINENTS


def _geo_base(ctx, scope: str) -> dict[str, Any]:
    center, zoom = SCOPES.get(scope, SCOPES["world"])
    return {
        "map": "world",
        "roam": True,
        "center": center,
        "zoom": zoom,
        "itemStyle": {
            "areaColor": ctx["tokens"]["grid"],
            "borderColor": ctx["tokens"]["bg"],
            "borderWidth": 0.6,
        },
        "emphasis": {"itemStyle": {"areaColor": rgba(ctx["colors"][0], 0.35)}, "label": {"show": False}},
        "label": {"show": False},
    }


def r_choropleth(frame, f, o, ctx):
    location, y = f["location"], f.get("y")
    data, value = d._aggregate(frame, [location], y, o.get("aggregation", "sum"))
    lookup = world_names()
    data["_country"] = data[location].astype(str).str.strip().str.lower().map(lookup)
    missing = data[data["_country"].isna()][location].astype(str).tolist()
    if missing:
        ctx["notes"].append(
            f"{len(missing)} location(s) were not recognised as countries: {', '.join(missing[:5])}."
        )
    matched = data.dropna(subset=["_country"])
    if matched.empty:
        raise AnalysisError("No locations matched country names or ISO-3 codes.")
    option = base(trigger="item", legend=False)
    option.pop("grid")
    option["visualMap"] = {
        "min": num(matched[value].min()),
        "max": num(matched[value].max()),
        "calculable": True,
        "left": 10,
        "bottom": 10,
        "text": [d._value_label(y, o), ""],
        "inRange": {"color": ctx["seq"]},
    }
    option["series"] = [
        {
            "type": "map",
            "name": d._value_label(y, o),
            **_geo_base(ctx, o.get("scope", "world")),
            "data": [
                {"name": n, "value": num(v)} for n, v in zip(matched["_country"], matched[value], strict=True)
            ],
        }
    ]
    best = matched.loc[matched[value].idxmax()]
    _insight(
        ctx,
        (
            f"{best[location]} has the highest value ({compact(float(best[value]))}); darker countries "
            "are higher."
        ),
    )
    return option


def r_scatter_geo(frame, f, o, ctx):
    data = frame.copy()
    data[f["lat"]], data[f["lon"]] = d._numeric(data, f["lat"]), d._numeric(data, f["lon"])
    data = data.dropna(subset=[f["lat"], f["lon"]])
    data = data[data[f["lat"]].between(-90, 90) & data[f["lon"]].between(-180, 180)]
    size, color = f.get("size"), f.get("color")
    if color and ctx["kinds"].get(color) != "numeric":
        data = _limit_series(data, color, ctx)
    data = d._sample(data, ctx["max_points"], ctx)
    option = base(trigger="item")
    option.pop("grid")
    option["geo"] = _geo_base(ctx, o.get("scope", "world"))
    sizes = _size_scale(d._numeric(data, size)) if size else None
    groups = (
        [(None, data)]
        if not color or ctx["kinds"].get(color) == "numeric"
        else [(g, data[data[color] == g]) for g in _groups_in_order(data, color)]
    )
    for group, part in groups:
        option["series"].append(
            {
                "type": "scatter",
                "coordinateSystem": "geo",
                "name": group or "Locations",
                "data": [
                    {
                        "value": [num(lon), num(lat)],
                        **({"symbolSize": sizes[data.index.get_loc(i)]} if sizes else {}),
                    }
                    for i, lat, lon in zip(part.index, part[f["lat"]], part[f["lon"]], strict=True)
                ],
                "symbolSize": 8,
                "itemStyle": {"opacity": 0.8, "borderColor": "#fff", "borderWidth": 0.5},
            }
        )
    option["legend"]["show"] = len(groups) > 1
    return option


# --- Dimensional -------------------------------------------------------------------------------


def _grid3d(ctx: dict[str, Any], names: tuple[str, str, str]) -> dict[str, Any]:
    option = base(trigger="item")
    option.pop("grid")
    option["grid3D"] = {
        "viewControl": {"projection": "perspective", "autoRotate": False, "distance": 220},
        "axisPointer": {"show": False},
        "environment": "auto",
        "boxWidth": 100,
        "boxDepth": 100,
        "boxHeight": 80,
    }
    option["xAxis3D"], option["yAxis3D"], option["zAxis3D"] = (
        {"type": "value", "name": name, "scale": True} for name in names
    )
    return option


def r_scatter_3d(frame, f, o, ctx):
    data = d._xy(frame, f, ctx, ("z", "size"))
    color = f.get("color")
    if color and ctx["kinds"].get(color) != "numeric":
        data = _limit_series(data, color, ctx)
    option = _grid3d(ctx, (f["x"], f["y"], f["z"]))
    sizes = _size_scale(data[f["size"]]) if f.get("size") else None
    groups = (
        [(None, data)]
        if not color or ctx["kinds"].get(color) == "numeric"
        else [(g, data[data[color] == g]) for g in _groups_in_order(data, color)]
    )
    for group, part in groups:
        option["series"].append(
            {
                "type": "scatter3D",
                "name": group or "Rows",
                "data": [
                    {
                        "value": [num(a), num(b), num(c)],
                        **({"symbolSize": sizes[data.index.get_loc(i)]} if sizes else {}),
                    }
                    for i, a, b, c in zip(part.index, part[f["x"]], part[f["y"]], part[f["z"]], strict=True)
                ],
                "symbolSize": 5,
                "itemStyle": {"opacity": float(o.get("opacity", 0.8))},
            }
        )
    option["legend"]["show"] = len(groups) > 1
    return option


def r_surface_3d(frame, f, o, ctx):
    import numpy as np
    from scipy.interpolate import griddata

    data = d._require_numeric(frame, [f["x"], f["y"], f["z"]], 4)
    data = data.sample(min(len(data.index), 5000), random_state=0)
    xs = np.linspace(data[f["x"]].min(), data[f["x"]].max(), 50)
    ys = np.linspace(data[f["y"]].min(), data[f["y"]].max(), 50)
    mesh_x, mesh_y = np.meshgrid(xs, ys)
    zs = griddata(
        data[[f["x"], f["y"]]].to_numpy(), data[f["z"]].to_numpy(), (mesh_x, mesh_y), method="linear"
    )
    ctx["notes"].append("The surface is interpolated from the data points.")
    option = _grid3d(ctx, (f["x"], f["y"], f["z"]))
    finite = zs[~np.isnan(zs)]
    option["visualMap"] = {
        "show": True,
        "dimension": 2,
        "min": num(finite.min()) if len(finite) else 0,
        "max": num(finite.max()) if len(finite) else 1,
        "inRange": {"color": ctx["seq"]},
        "right": 0,
        "top": "middle",
        "calculable": True,
    }
    option["series"] = [
        {
            "type": "surface",
            "name": f["z"],
            "wireframe": {
                "show": bool(o.get("contours", True)),
                "lineStyle": {"color": rgba("#000000", 0.15)},
            },
            "data": [[num(xs[j]), num(ys[i]), num(zs[i, j])] for i in range(len(ys)) for j in range(len(xs))],
        }
    ]
    return option


def _cluster_scatter(
    points: Any, label_values: Any, names: tuple[str, str], ctx: dict[str, Any], title: str | None = None
) -> dict[str, Any]:
    option = base(trigger="item")
    option["xAxis"] = value_axis(names[0], scale=True)
    option["yAxis"] = value_axis(names[1], scale=True)
    if label_values is None:
        option["series"] = [
            {
                "type": "scatter",
                "name": title or "Rows",
                "data": [[num(a), num(b)] for a, b in points],
                "symbolSize": 7,
                "itemStyle": {"opacity": 0.75},
                "large": len(points) > 2000,
            }
        ]
        option["legend"]["show"] = False
        return option
    import pandas as pd

    frame = pd.DataFrame({"a": points[:, 0], "b": points[:, 1], "label": list(label_values)})
    for label in labels(frame["label"].value_counts().index[:12]):
        part = frame[frame["label"].astype(str) == label]
        option["series"].append(
            {
                "type": "scatter",
                "name": label,
                "data": [[num(a), num(b)] for a, b in zip(part["a"], part["b"], strict=True)],
                "symbolSize": 7,
                "itemStyle": {"opacity": 0.78},
                "emphasis": {"focus": "series"},
            }
        )
    return option


def r_pca_2d(frame, f, o, ctx):
    model, scores, label_values, _ = d._pca(frame, f, o, ctx, 2)
    option = _cluster_scatter(scores[:, :2], label_values, (d._axis(model, 0), d._axis(model, 1)), ctx)
    explained = model.explained_variance_ratio_[:2].sum()
    _stat(ctx, "Variance explained (2 PCs)", f"{explained:.1%}", "text")
    _insight(
        ctx,
        (
            f"Two components keep {explained:.0%} of the information in {model.n_features_in_} "
            "measures; nearby points are similar rows."
        ),
    )
    return option


def r_pca_3d(frame, f, o, ctx):
    model, scores, label_values, _ = d._pca(frame, f, o, ctx, 3)
    if scores.shape[1] < 3:
        raise AnalysisError("A 3D PCA map needs at least three numeric columns.")
    names = tuple(d._axis(model, i) for i in range(3))
    option = _grid3d(ctx, names)
    groups = (
        [(None, list(range(len(scores))))]
        if label_values is None
        else [
            (label, [i for i, value in enumerate(label_values) if str(value) == label])
            for label in labels(label_values.value_counts().index[:12])
        ]
    )
    for group, rows in groups:
        option["series"].append(
            {
                "type": "scatter3D",
                "name": group or "Rows",
                "data": [nums(scores[i, :3], 5) for i in rows],
                "symbolSize": 5,
            }
        )
    option["legend"]["show"] = len(groups) > 1
    _stat(ctx, "Variance explained (3 PCs)", f"{model.explained_variance_ratio_[:3].sum():.1%}", "text")
    return option


def r_pca_biplot(frame, f, o, ctx):
    model, scores, label_values, columns = d._pca(frame, f, o, ctx, 2)
    option = _cluster_scatter(scores[:, :2], label_values, (d._axis(model, 0), d._axis(model, 1)), ctx)
    scale = float(abs(scores[:, :2]).max()) or 1
    arrows = [
        {
            "coords": [[0, 0], [num(model.components_[0, i] * scale), num(model.components_[1, i] * scale)]],
            "name": column,
        }
        for i, column in enumerate(columns[:20])
    ]
    option["series"].append(
        {
            "type": "lines",
            "coordinateSystem": "cartesian2d",
            "name": "Loadings",
            "data": arrows,
            "symbol": ["none", "arrow"],
            "symbolSize": 9,
            "lineStyle": {"color": NEGATIVE, "width": 2, "opacity": 0.9},
            "label": {"show": True, "position": "end", "formatter": "{b}", "color": NEGATIVE},
            "z": 10,
        }
    )
    top = columns[int(abs(model.components_[0]).argmax())]
    _insight(
        ctx,
        f"{top} drives the first component the most; arrows pointing the same way are correlated measures.",
    )
    return option


def r_scree_plot(frame, f, o, ctx):
    import numpy as np
    from sklearn.decomposition import PCA

    values, _, columns = d._matrix(frame, f, ctx, bool(o.get("standardize", True)))
    model = PCA(random_state=0).fit(values)
    ratio = model.explained_variance_ratio_[:20]
    option = base()
    option["tooltip"]["axisPointer"] = {"type": "shadow"}
    option["xAxis"] = category_axis([f"PC{i + 1}" for i in range(len(ratio))])
    option["yAxis"] = [
        {"type": "value", "name": "Explained", "axisLabel": {"formatter": "{value}%"}},
        {"type": "value", "max": 100, "axisLabel": {"formatter": "{value}%"}, "splitLine": {"show": False}},
    ]
    option["series"] = [
        {
            "type": "bar",
            "name": "Explained",
            "data": nums(ratio * 100, 2),
            "itemStyle": {"borderRadius": [4, 4, 0, 0]},
        },
        {
            "type": "line",
            "name": "Cumulative",
            "yAxisIndex": 1,
            "data": nums(np.cumsum(ratio) * 100, 2),
            "lineStyle": {"width": 3},
        },
    ]
    option["tooltip"]["valueFormatter"] = fn("percent", digits=1)
    needed = int(np.searchsorted(np.cumsum(model.explained_variance_ratio_), 0.9) + 1)
    _stat(ctx, "Components for 90% variance", f"{needed} of {len(columns)}", "text")
    degree = "highly" if needed <= len(columns) / 3 else "moderately"
    _insight(
        ctx,
        f"{needed} of {len(columns)} components keep 90% of the variance; the data is {degree} compressible.",
    )
    return option


def r_tsne_2d(frame, f, o, ctx):
    from sklearn.manifold import TSNE

    values, label_values, _ = d._matrix(frame, f, ctx, True, 2000)
    perplexity = min(float(o.get("perplexity", 30)), max(2.0, (len(values) - 1) / 3))
    embedding = TSNE(
        n_components=2, perplexity=perplexity, init="pca", random_state=0, learning_rate="auto"
    ).fit_transform(values)
    _insight(
        ctx,
        (
            "Points that sit close together are similar rows; distances between far-apart clusters are "
            "not meaningful in t-SNE."
        ),
    )
    return _cluster_scatter(embedding, label_values, ("t-SNE 1", "t-SNE 2"), ctx)


def r_lda_2d(frame, f, o, ctx):
    import numpy as np
    from sklearn.discriminant_analysis import LinearDiscriminantAnalysis

    values, label_values, _ = d._matrix(frame, f, ctx, True, ctx["max_points"])
    if label_values.nunique() < 2:
        raise AnalysisError("LDA needs a label with at least two groups.")
    components = min(2, label_values.nunique() - 1, values.shape[1])
    model = LinearDiscriminantAnalysis(n_components=components).fit(values, label_values)
    projected = model.transform(values)
    if components == 1:
        projected = np.column_stack(
            [projected[:, 0], np.random.default_rng(0).uniform(-0.3, 0.3, len(projected))]
        )
    accuracy = model.score(values, label_values)
    _stat(ctx, "Training accuracy", f"{accuracy:.1%}", "text")
    _insight(
        ctx,
        (
            f"The projection separates the {f['color']} groups with {accuracy:.0%} training accuracy; "
            "well-separated colours mean the measures distinguish the groups."
        ),
    )
    return _cluster_scatter(projected, label_values, ("LD1", "LD2" if components == 2 else "(jitter)"), ctx)


def r_kmeans_clusters(frame, f, o, ctx):
    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA

    values, _, _ = d._matrix(frame, f, ctx, bool(o.get("standardize", True)), ctx["max_points"])
    k = int(o.get("clusters") or 0) or d._choose_k(values)[0]
    model = KMeans(n_clusters=k, n_init=10, random_state=0).fit(values)
    pca = PCA(n_components=2, random_state=0).fit(values)
    points, centres = pca.transform(values), pca.transform(model.cluster_centers_)
    import pandas as pd

    names = pd.Series([f"Cluster {label + 1}" for label in model.labels_])
    option = _cluster_scatter(points, names, (d._axis(pca, 0), d._axis(pca, 1)), ctx)
    option["series"].append(
        {
            "type": "scatter",
            "name": "Centres",
            "data": [nums(c, 5) for c in centres],
            "symbol": "diamond",
            "symbolSize": 18,
            "itemStyle": {"color": ctx["tokens"]["fg"], "borderColor": ctx["tokens"]["bg"], "borderWidth": 2},
            "z": 10,
        }
    )
    sizes = np.bincount(model.labels_)
    _stat(ctx, "Clusters", k, "integer")
    _insight(
        ctx,
        (
            f"{k} clusters found; the largest holds {sizes.max() / len(values):.0%} of rows. Diamonds "
            "mark each cluster's centre."
        ),
    )
    return option


# --- Model evaluation --------------------------------------------------------------------------


def r_confusion_matrix(frame, f, o, ctx):
    from sklearn.metrics import accuracy_score, confusion_matrix, f1_score

    data = frame[[f["actual"], f["predicted"]]].dropna()
    actual, predicted = d._label(data[f["actual"]]), d._label(data[f["predicted"]])
    names = sorted(set(actual) | set(predicted))[:30]
    normalize = o.get("normalize", "none")
    matrix = confusion_matrix(
        actual, predicted, labels=names, normalize=None if normalize == "none" else normalize
    )
    scale = 100 if normalize != "none" else 1
    cells = [[j, i, num(matrix[i, j] * scale, 1)] for i in range(len(names)) for j in range(len(names))]
    option = _heatmap(names, names, cells, ctx, name="Rows" if normalize == "none" else "%")
    option["xAxis"]["name"], option["yAxis"]["name"] = "Predicted", "Actual"
    accuracy = accuracy_score(actual, predicted)
    _stat(ctx, "Accuracy", f"{accuracy:.1%}", "text")
    _stat(ctx, "Macro F1", round(float(f1_score(actual, predicted, average="macro", zero_division=0)), 4))
    _insight(
        ctx,
        (
            f"{accuracy:.0%} of rows are predicted correctly (the diagonal); off-diagonal cells show "
            "which classes get confused."
        ),
    )
    return option


def _unit_axes(option: dict[str, Any], x_name: str, y_name: str) -> None:
    option["xAxis"] = {
        "type": "value",
        "min": 0,
        "max": 1,
        "name": x_name,
        "nameLocation": "middle",
        "nameGap": 30,
    }
    option["yAxis"] = {
        "type": "value",
        "min": 0,
        "max": 1,
        "name": y_name,
        "nameLocation": "middle",
        "nameGap": 40,
    }


def _thin(xs: Any, ys: Any, limit: int = 400) -> list[list[float | None]]:
    step = max(1, len(xs) // limit)
    return [[num(a, 5), num(b, 5)] for a, b in list(zip(xs, ys, strict=True))[::step]] + [
        [num(xs[-1], 5), num(ys[-1], 5)]
    ]


def r_roc_curve(frame, f, o, ctx):
    from sklearn.metrics import roc_auc_score, roc_curve

    truth, score, positive = d._truth_and_score(frame, f, o, ctx)
    fpr, tpr, _ = roc_curve(truth, score)
    auc = roc_auc_score(truth, score)
    option = base()
    _unit_axes(option, "False positive rate", "True positive rate")
    option["series"] = [
        {
            "type": "line",
            "name": f"ROC (AUC = {auc:.3f})",
            "data": _thin(fpr, tpr),
            "showSymbol": False,
            "areaStyle": {"opacity": 0.15},
            "lineStyle": {"width": 3},
        },
        {
            "type": "line",
            "name": "Random",
            "data": [[0, 0], [1, 1]],
            "showSymbol": False,
            "lineStyle": {"type": "dashed", "color": ctx["tokens"]["muted"]},
        },
    ]
    _stat(ctx, "ROC AUC", round(float(auc), 4))
    _insight(
        ctx,
        (
            f"AUC = {auc:.3f}: the model ranks a random “{positive}” above a random other row {auc:.0%} "
            "of the time (0.5 = guessing)."
        ),
    )
    return option


def r_pr_curve(frame, f, o, ctx):
    from sklearn.metrics import average_precision_score, precision_recall_curve

    truth, score, _ = d._truth_and_score(frame, f, o, ctx)
    precision, recall, _ = precision_recall_curve(truth, score)
    ap = average_precision_score(truth, score)
    prevalence = float(truth.mean())
    option = base()
    _unit_axes(option, "Recall", "Precision")
    option["series"] = [
        {
            "type": "line",
            "name": f"PR (AP = {ap:.3f})",
            "data": _thin(recall, precision),
            "showSymbol": False,
            "areaStyle": {"opacity": 0.15},
            "lineStyle": {"width": 3},
            "itemStyle": {"color": ctx["colors"][1]},
            "markLine": {
                "silent": True,
                "symbol": "none",
                "data": [{"yAxis": num(prevalence, 4), "name": "Baseline"}],
                "lineStyle": {"type": "dashed", "color": ctx["tokens"]["muted"]},
                "label": {"formatter": "Baseline {c}"},
            },
        }
    ]
    _stat(ctx, "Average precision", round(float(ap), 4))
    _insight(
        ctx,
        (
            f"Average precision is {ap:.2f} versus a {prevalence:.2f} baseline; the curve shows the "
            "precision you keep as you catch more positives."
        ),
    )
    return option


def r_calibration_curve(frame, f, o, ctx):
    from sklearn.calibration import calibration_curve
    from sklearn.metrics import brier_score_loss

    truth, score, _ = d._truth_and_score(frame, f, o, ctx)
    if ctx.get("scores_scaled"):
        score = (score - score.min()) / ((score.max() - score.min()) or 1)
        ctx["notes"].append("Scores were outside 0–1 and were rescaled for calibration.")
    observed, predicted = calibration_curve(truth, score, n_bins=int(o.get("bins", 10)), strategy="quantile")
    option = base()
    _unit_axes(option, "Mean predicted probability", "Observed positive rate")
    option["series"] = [
        {
            "type": "line",
            "name": "Perfectly calibrated",
            "data": [[0, 0], [1, 1]],
            "showSymbol": False,
            "lineStyle": {"type": "dashed", "color": ctx["tokens"]["muted"]},
        },
        {
            "type": "line",
            "name": "Model",
            "data": [[num(a, 4), num(b, 4)] for a, b in zip(predicted, observed, strict=True)],
            "symbolSize": 9,
            "lineStyle": {"width": 3},
        },
    ]
    brier = brier_score_loss(truth, score)
    _stat(ctx, "Brier score", round(float(brier), 4))
    _insight(
        ctx,
        (
            "Points on the dashed line mean predicted probabilities match reality; the Brier score is "
            f"{brier:.3f} (lower is better)."
        ),
    )
    return option


def r_cumulative_gain(frame, f, o, ctx):
    import numpy as np

    truth, score, _ = d._truth_and_score(frame, f, o, ctx)
    order = np.argsort(-score, kind="mergesort")
    hits = np.cumsum(truth[order])
    population = np.arange(1, len(truth) + 1) / len(truth)
    gain = hits / max(truth.sum(), 1)
    lift = gain / population
    option = base()
    option["grid"] = [
        {"left": 50, "right": "54%", "top": 50, "bottom": 40},
        {"left": "56%", "right": 20, "top": 50, "bottom": 40},
    ]
    option["title"] = [
        {"text": "Cumulative gain", "left": 50, "top": 22},
        {"text": "Lift", "left": "56%", "top": 22},
    ]
    option["xAxis"] = [
        {
            "type": "value",
            "min": 0,
            "max": 1,
            "gridIndex": i,
            "name": "Share of population targeted",
            "nameLocation": "middle",
            "nameGap": 26,
            "axisLabel": {"formatter": fn("percent", digits=0, scale=100)},
        }
        for i in range(2)
    ]
    option["yAxis"] = [
        {
            "type": "value",
            "min": 0,
            "max": 1,
            "gridIndex": 0,
            "axisLabel": {"formatter": fn("percent", digits=0, scale=100)},
        },
        {"type": "value", "gridIndex": 1, "scale": True},
    ]
    option["series"] = [
        {
            "type": "line",
            "name": "Model",
            "data": _thin(population, gain),
            "showSymbol": False,
            "lineStyle": {"width": 3},
        },
        {
            "type": "line",
            "name": "Random",
            "data": [[0, 0], [1, 1]],
            "showSymbol": False,
            "lineStyle": {"type": "dashed", "color": ctx["tokens"]["muted"]},
        },
        {
            "type": "line",
            "name": "Lift",
            "xAxisIndex": 1,
            "yAxisIndex": 1,
            "data": _thin(population, lift),
            "showSymbol": False,
            "lineStyle": {"width": 3},
            "itemStyle": {"color": ctx["colors"][2]},
            "markLine": {
                "silent": True,
                "symbol": "none",
                "data": [{"yAxis": 1}],
                "lineStyle": {"type": "dashed", "color": ctx["tokens"]["muted"]},
            },
        },
    ]
    decile = int(max(1, len(truth) // 10))
    _stat(ctx, "Lift in top 10%", round(float(lift[decile - 1]), 3))
    _insight(
        ctx,
        (
            f"Targeting the top 10% of scores captures {gain[decile - 1]:.0%} of positives — "
            f"{lift[decile - 1]:.1f}× better than random."
        ),
    )
    return option


def r_threshold_metrics(frame, f, o, ctx):
    import numpy as np
    from sklearn.metrics import precision_recall_curve

    truth, score, _ = d._truth_and_score(frame, f, o, ctx)
    precision, recall, thresholds = precision_recall_curve(truth, score)
    precision, recall = precision[:-1], recall[:-1]
    f1 = np.where(precision + recall > 0, 2 * precision * recall / np.maximum(precision + recall, 1e-12), 0)
    best = int(np.argmax(f1))
    option = base()
    option["xAxis"] = value_axis("Threshold", scale=True)
    option["yAxis"] = {
        "type": "value",
        "min": 0,
        "max": 1,
        "name": "Score",
        "nameLocation": "middle",
        "nameGap": 40,
    }
    option["series"] = [
        {
            "type": "line",
            "name": name,
            "data": _thin(thresholds, values),
            "showSymbol": False,
            "lineStyle": {"width": 2.6},
        }
        for values, name in ((precision, "Precision"), (recall, "Recall"), (f1, "F1"))
    ]
    option["series"][2]["markLine"] = {
        "silent": True,
        "symbol": "none",
        "data": [{"xAxis": num(thresholds[best], 4)}],
        "lineStyle": {"color": NEGATIVE, "type": "dotted"},
        "label": {"formatter": f"Best F1 {f1[best]:.3f}"},
    }
    _stat(ctx, "Best threshold", round(float(thresholds[best]), 4))
    _stat(ctx, "Best F1", round(float(f1[best]), 4))
    _insight(
        ctx,
        f"A threshold of {thresholds[best]:.3f} balances precision and recall best (F1 = {f1[best]:.3f}).",
    )
    return option


def r_predicted_vs_actual(frame, f, o, ctx):
    actual, predicted = d._regression_pairs(frame, f, ctx)
    low, high = float(min(actual.min(), predicted.min())), float(max(actual.max(), predicted.max()))
    option = base(trigger="item")
    option["xAxis"] = value_axis(f"Actual {f['actual']}", scale=True)
    option["yAxis"] = value_axis(f"Predicted {f['predicted']}", scale=True)
    option["series"] = [
        {
            "type": "scatter",
            "name": "Rows",
            "data": [[num(a), num(b)] for a, b in zip(actual, predicted, strict=True)],
            "symbolSize": 6,
            "itemStyle": {"opacity": 0.65},
            "large": len(actual) > 2000,
        },
        {
            "type": "line",
            "name": "Perfect prediction",
            "data": [[num(low), num(low)], [num(high), num(high)]],
            "showSymbol": False,
            "lineStyle": {"type": "dashed", "color": NEGATIVE, "width": 2},
        },
    ]
    _insight(
        ctx,
        (
            "Points close to the red dashed line are accurate predictions; points above it are "
            "over-predicted, below under-predicted."
        ),
    )
    return option


def r_residual_plot(frame, f, o, ctx):
    actual, predicted = d._regression_pairs(frame, f, ctx)
    residual = actual - predicted
    option = base(trigger="item", legend=False)
    option["xAxis"] = value_axis("Predicted", scale=True)
    option["yAxis"] = value_axis("Residual (actual − predicted)", scale=True)
    option["series"] = [
        {
            "type": "scatter",
            "name": "Residuals",
            "data": [
                {"value": [num(p), num(r)], "itemStyle": {"color": POSITIVE if r >= 0 else NEGATIVE}}
                for p, r in zip(predicted, residual, strict=True)
            ],
            "symbolSize": 6,
            "itemStyle": {"opacity": 0.65},
            "markLine": {
                "silent": True,
                "symbol": "none",
                "data": [{"yAxis": 0}],
                "lineStyle": {"type": "dashed", "color": ctx["tokens"]["muted"]},
            },
        }
    ]
    _insight(
        ctx,
        (
            f"Residuals average {compact(float(residual.mean()))}; a random cloud around zero means no "
            "systematic bias."
        ),
    )
    return option


def r_error_distribution(frame, f, o, ctx):
    import numpy as np

    actual, predicted = d._regression_pairs(frame, f, ctx)
    residual = actual - predicted
    edges = _bins(residual, int(o.get("bins") or 0) or None)
    counts, _ = np.histogram(residual, bins=edges)
    option = base(legend=False)
    option["tooltip"]["axisPointer"] = {"type": "shadow"}
    option["xAxis"] = category_axis(
        _bin_labels(edges), "Error (actual − predicted)", axisLabel={"rotate": 35, "hideOverlap": True}
    )
    option["yAxis"] = value_axis("Rows")
    option["series"] = [
        {
            "type": "bar",
            "name": "Errors",
            "data": [int(c) for c in counts],
            "barCategoryGap": "4%",
            "itemStyle": {"borderRadius": [3, 3, 0, 0]},
        }
    ]
    _insight(
        ctx,
        (
            f"Errors average {compact(float(residual.mean()))} with a typical size of "
            f"±{compact(float(np.abs(residual).mean()))}."
        ),
    )
    return option


def _importance_option(names: list[str], means: Any, stds: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    option = base(legend=False)
    option["tooltip"]["axisPointer"] = {"type": "shadow"}
    option["xAxis"] = value_axis("Importance (score drop when shuffled)")
    option["yAxis"] = category_axis(names)
    errors = [
        {
            "value": [num(m), i],
            "shapes": [
                {
                    "type": "polyline",
                    "points": [[m - s, i], [m + s, i]],
                    "color": ctx["tokens"]["muted"],
                    "width": 1.5,
                }
            ],
        }
        for i, (m, s) in enumerate(zip(means, stds, strict=True))
    ]
    option["series"] = [
        {
            "type": "bar",
            "name": "Importance",
            "data": [
                {
                    "value": num(m),
                    "itemStyle": {
                        "color": ctx["colors"][i % len(ctx["colors"])],
                        "borderRadius": [0, 4, 4, 0],
                    },
                }
                for i, m in enumerate(means)
            ],
        },
        shapes_series(errors, name="Spread", silent=True, tooltip={"show": False}, z=5),
    ]
    return option


def r_feature_importance(frame, f, o, ctx):
    from sklearn.inspection import permutation_importance
    from sklearn.model_selection import train_test_split

    x, y, columns, regression = d._supervised(frame, f, ctx)
    stratify = None if regression or y.value_counts().min() < 2 else y
    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=0.25, random_state=0, stratify=stratify
    )
    model = d._estimator("random_forest", regression).fit(x_train, y_train)
    result = permutation_importance(model, x_test, y_test, n_repeats=5, random_state=0, n_jobs=1)
    order = result.importances_mean.argsort()[::-1][: int(o.get("top_n", 15))][::-1]
    option = _importance_option(
        [columns[i] for i in order], result.importances_mean[order], result.importances_std[order], ctx
    )
    score = float(model.score(x_test, y_test))
    _stat(ctx, "Model", "Random forest " + ("regressor" if regression else "classifier"), "text")
    _stat(ctx, "Hold-out score (R²)" if regression else "Hold-out accuracy", round(score, 4))
    _insight(
        ctx,
        (
            f"{columns[order[-1]]} matters most for predicting {f['target']}; longer bars mean the "
            "model gets worse without that input."
        ),
    )
    return option


def _band_series(xs: Any, mean: Any, sd: Any, name: str, colour: str, stack: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "line",
            "data": [[num(a), num(b)] for a, b in zip(xs, mean - sd, strict=True)],
            "stack": stack,
            "showSymbol": False,
            "lineStyle": {"opacity": 0},
            "tooltip": {"show": False},
            "silent": True,
        },
        {
            "type": "line",
            "data": [[num(a), num(b)] for a, b in zip(xs, 2 * sd, strict=True)],
            "stack": stack,
            "showSymbol": False,
            "lineStyle": {"opacity": 0},
            "areaStyle": {"color": rgba(colour, 0.18)},
            "tooltip": {"show": False},
            "silent": True,
        },
        {
            "type": "line",
            "name": name,
            "data": [[num(a), num(b)] for a, b in zip(xs, mean, strict=True)],
            "lineStyle": {"width": 3, "color": colour},
            "itemStyle": {"color": colour},
        },
    ]


def r_learning_curve(frame, f, o, ctx):
    import numpy as np
    from sklearn.model_selection import learning_curve

    x, y, _, regression = d._supervised(frame, f, ctx, 3000)
    model = d._estimator(o.get("model", "random_forest"), regression)
    sizes, train, test = learning_curve(
        model, x, y, cv=3, train_sizes=np.linspace(0.15, 1.0, 5), n_jobs=1, shuffle=True, random_state=0
    )
    option = base()
    option["xAxis"] = value_axis("Training rows", scale=True)
    option["yAxis"] = value_axis("R²" if regression else "Accuracy", scale=True)
    option["series"] = _band_series(
        sizes, train.mean(axis=1), train.std(axis=1), "Training score", ctx["colors"][0], "train"
    ) + _band_series(sizes, test.mean(axis=1), test.std(axis=1), "Validation score", ctx["colors"][2], "test")
    option["legend"]["data"] = ["Training score", "Validation score"]
    gap = float(train[-1].mean() - test[-1].mean())
    _stat(ctx, "Train/validation gap", round(gap, 4))
    _insight(
        ctx,
        f"The gap between training and validation is {gap:.2f}: "
        + (
            "more data or a simpler model would help (over-fitting)."
            if gap > 0.1
            else "the model generalises well."
        ),
    )
    return option


def r_elbow(frame, f, o, ctx):
    from sklearn.cluster import KMeans

    values, _, _ = d._matrix(frame, f, ctx, bool(o.get("standardize", True)), 5000)
    ks = list(range(1, min(int(o.get("max_k", 10)), len(values) - 1) + 1))
    inertia = [float(KMeans(n_clusters=k, n_init=4, random_state=0).fit(values).inertia_) for k in ks]
    first, last = (ks[0], inertia[0]), (ks[-1], inertia[-1])
    distances = [
        abs((last[1] - first[1]) * k - (last[0] - first[0]) * i + last[0] * first[1] - last[1] * first[0])
        for k, i in zip(ks, inertia, strict=True)
    ]
    elbow = ks[distances.index(max(distances))]
    option = base(legend=False)
    option["xAxis"] = {
        "type": "value",
        "name": "Number of clusters (k)",
        "nameLocation": "middle",
        "nameGap": 28,
        "minInterval": 1,
        "min": 1,
        "max": ks[-1],
    }
    option["yAxis"] = value_axis("Inertia (within-cluster sum of squares)")
    option["series"] = [
        {
            "type": "line",
            "name": "Inertia",
            "data": [[k, num(i)] for k, i in zip(ks, inertia, strict=True)],
            "symbolSize": 9,
            "lineStyle": {"width": 3},
            "markLine": {
                "silent": True,
                "symbol": "none",
                "data": [{"xAxis": elbow}],
                "lineStyle": {"color": NEGATIVE, "type": "dotted"},
                "label": {"formatter": f"Elbow at k = {elbow}"},
            },
        }
    ]
    _stat(ctx, "Suggested k", elbow, "integer")
    _insight(
        ctx, f"The curve bends at k = {elbow}: adding more clusters after that brings little improvement."
    )
    return option


def r_silhouette(frame, f, o, ctx):
    values, _, _ = d._matrix(frame, f, ctx, bool(o.get("standardize", True)), 5000)
    best, scores = d._choose_k(values, int(o.get("max_k", 10)))
    option = base(legend=False)
    option["tooltip"]["axisPointer"] = {"type": "shadow"}
    option["xAxis"] = category_axis([k for k, _ in scores], "Number of clusters (k)")
    option["yAxis"] = value_axis("Mean silhouette score")
    option["series"] = [
        {
            "type": "bar",
            "data": [
                {
                    "value": num(s, 4),
                    "itemStyle": {
                        "color": POSITIVE if k == best else ctx["colors"][0],
                        "borderRadius": [4, 4, 0, 0],
                    },
                }
                for k, s in scores
            ],
        }
    ]
    _stat(ctx, "Best k", best, "integer")
    _stat(ctx, "Best silhouette", round(max(s for _, s in scores), 4))
    _insight(
        ctx,
        (
            f"k = {best} gives the clearest separation (silhouette {max(s for _, s in scores):.2f}; "
            "above 0.5 is strong)."
        ),
    )
    return option


# --- KPI, gauge and decomposition ----------------------------------------------------------------


def r_kpi_card(frame, f, o, ctx):
    data, values, _ = d._series_frame(frame, f, o, [f["y"]])
    value, x = values[0], f["x"]
    series = data[[x, value]].dropna()
    if series.empty:
        raise AnalysisError(f"{value} has no values to summarise.")
    latest = float(series[value].iloc[-1])
    previous = float(series[value].iloc[-2]) if len(series.index) > 1 else float("nan")
    change = (latest - previous) / abs(previous) if previous and not math.isnan(previous) else float("nan")
    up = not math.isnan(change) and change >= 0
    colour = POSITIVE if up else NEGATIVE
    arrow = "▲" if up else "▼"
    delta = "" if math.isnan(change) else f"{arrow} {change:+.1%} vs previous period"
    option = base(legend=False)
    option["grid"] = {"left": 8, "right": 8, "top": "58%", "bottom": 8}
    option["xAxis"] = {"type": "time", "show": False}
    option["yAxis"] = {"type": "value", "show": False, "scale": True}
    option["graphic"] = [
        {
            "type": "text",
            "left": 16,
            "top": 12,
            "style": {
                "text": d._value_label(value, o).strip(),
                "fill": ctx["tokens"]["muted"],
                "font": "600 13px sans-serif",
            },
        },
        {
            "type": "text",
            "left": 16,
            "top": 36,
            "style": {"text": compact(latest), "fill": ctx["tokens"]["fg"], "font": "700 44px sans-serif"},
        },
        {
            "type": "text",
            "left": 16,
            "top": 96,
            "style": {"text": delta, "fill": colour, "font": "600 14px sans-serif"},
        },
        {
            "type": "text",
            "right": 16,
            "top": 16,
            "style": {
                "text": str(series[x].iloc[-1])[:10],
                "fill": ctx["tokens"]["muted"],
                "font": "12px sans-serif",
            },
        },
    ]
    option["series"] = [
        {
            "type": "line",
            "name": value,
            "data": [[s_, num(v)] for s_, v in zip(dates(series[x]), series[value], strict=True)],
            "showSymbol": False,
            "smooth": True,
            "lineStyle": {"width": 2.4, "color": ctx["colors"][0]},
            "areaStyle": {"color": rgba(ctx["colors"][0], 0.18)},
        }
    ]
    _stat(ctx, "Latest", round(latest, 4))
    if not math.isnan(change):
        _stat(ctx, "Change", round(change * 100, 2), "percent_value")
    _insight(
        ctx,
        f"{value} is {compact(latest)} in the latest period"
        + ("" if math.isnan(change) else f", {change:+.1%} versus the period before."),
    )
    return option


def r_gauge(frame, f, o, ctx):
    how = o.get("aggregation", "sum")
    values = d._numeric(frame, f["y"]).dropna()
    if values.empty:
        raise AnalysisError(f"{f['y']} has no numeric values.")
    current = float(getattr(values, how)() if how != "count" else values.count())
    goal = float(o.get("goal") or 0)
    if not goal and f.get("target"):
        targets = d._numeric(frame, f["target"]).dropna()
        goal = float(getattr(targets, how)() if how != "count" else targets.count()) if len(targets) else 0.0
    maximum = goal * 1.25 if goal > 0 else (current * 1.5 if current > 0 else 1)
    share = current / goal if goal else float("nan")
    option = base(trigger="item", legend=False)
    option.pop("grid")
    bands = [[0.6, NEGATIVE], [0.9, "#F59E0B"], [1, POSITIVE]] if goal else [[1, ctx["colors"][0]]]
    option["series"] = [
        {
            "type": "gauge",
            "name": f["y"],
            "min": 0,
            "max": num(maximum),
            "progress": {"show": True, "width": 16, "itemStyle": {"color": ctx["colors"][0]}},
            "axisLine": {
                "lineStyle": {
                    "width": 16,
                    "color": [[b[0] * (goal / maximum if goal else 1), b[1]] for b in bands]
                    + ([[1, ctx["tokens"]["grid"]]] if goal else []),
                }
            },
            "pointer": {"width": 5, "itemStyle": {"color": ctx["tokens"]["fg"]}},
            "axisTick": {"show": False},
            "splitLine": {"length": 10, "lineStyle": {"color": ctx["tokens"]["muted"]}},
            "axisLabel": {"color": ctx["tokens"]["muted"], "formatter": fn("compact")},
            "title": {"offsetCenter": [0, "72%"], "color": ctx["tokens"]["muted"]},
            "detail": {
                "valueAnimation": True,
                "offsetCenter": [0, "42%"],
                "fontSize": 30,
                "fontWeight": 700,
                "color": ctx["tokens"]["fg"],
                "formatter": fn("compact"),
            },
            "data": [
                {
                    "value": num(current),
                    "name": f"{d._value_label(f['y'], o).strip()}"
                    + (f" · target {compact(goal)}" if goal else ""),
                }
            ],
        }
    ]
    _stat(ctx, "Value", round(current, 4))
    if goal:
        _stat(ctx, "Of target", f"{share:.0%}", "text")
        _insight(
            ctx,
            f"{compact(current)} is {share:.0%} of the {compact(goal)} target"
            + (" — on track." if share >= 0.9 else " — behind target."),
        )
    return option


def r_decomposition_tree(frame, f, o, ctx):
    path, y = f["path"][:5], f.get("y")
    if not path:
        raise AnalysisError("Choose at least one column to break down by.")
    data, value = d._aggregate(frame, path, y, o.get("aggregation", "sum"))
    total = float(data[value].sum())
    top = int(o.get("top_n", 6))

    def branch(subset, depth: int) -> list[dict[str, Any]]:
        if depth >= len(path):
            return []
        grouped = subset.groupby(path[depth], sort=False)[value].sum().sort_values(ascending=False)
        nodes = []
        for name, amount in grouped.head(top).items():
            share = float(amount) / total if total else 0
            node: dict[str, Any] = {
                "name": f"{name}\n{compact(float(amount))} · {share:.0%}",
                "value": num(amount),
                "symbolSize": round(8 + 26 * math.sqrt(max(share, 0)), 1),
            }
            children = branch(subset[subset[path[depth]].astype(str) == str(name)], depth + 1)
            if children:
                node["children"] = children
            nodes.append(node)
        rest = grouped.iloc[top:].sum()
        if len(grouped) > top and rest:
            nodes.append(
                {
                    "name": f"Other ({len(grouped) - top})\n{compact(float(rest))}",
                    "value": num(rest),
                    "symbolSize": 8,
                }
            )
        return nodes

    root = {
        "name": f"Total\n{compact(total)}",
        "value": num(total),
        "symbolSize": 34,
        "children": branch(data, 0),
    }
    option = base(trigger="item", legend=False)
    option.pop("grid")
    option["series"] = [
        {
            "type": "tree",
            "data": [root],
            "orient": "LR",
            "top": 20,
            "bottom": 20,
            "left": 70,
            "right": 150,
            "initialTreeDepth": 2,
            "expandAndCollapse": True,
            "roam": True,
            "symbol": "circle",
            "itemStyle": {"color": ctx["colors"][0], "borderColor": ctx["colors"][0]},
            "lineStyle": {"color": ctx["tokens"]["line"], "width": 1.5, "curveness": 0.5},
            "label": {
                "position": "left",
                "verticalAlign": "middle",
                "align": "right",
                "fontSize": 11,
                "color": ctx["tokens"]["fg"],
            },
            "leaves": {"label": {"position": "right", "align": "left"}},
        }
    ]
    first = data.groupby(path[0])[value].sum().sort_values(ascending=False)
    if len(first) and total:
        _insight(
            ctx,
            f"{first.index[0]} contributes the most ({first.iloc[0] / total:.0%} of {compact(total)}); "
            "click a node to drill further.",
        )
    return option


RENDERERS: dict[str, Any] = {
    name[2:]: value for name, value in globals().items() if name.startswith("r_") and callable(value)
}


# --- Context, titles and explanations ----------------------------------------------------------


def context(style: dict[str, Any], limits: dict[str, Any], kinds: dict[str, str]) -> dict[str, Any]:
    palette = dict(
        catalog.PALETTES.get(style.get("palette") or "", catalog.PALETTES[catalog.DEFAULT_PALETTE])
    )
    custom = [str(c) for c in (style.get("colors") or []) if isinstance(c, str) and HEX_COLOR.match(c)][:12]
    if custom:
        palette["colors"] = custom + [c for c in palette["colors"] if c not in custom]
    theme = style.get("theme") if style.get("theme") in catalog.THEMES else "light"
    try:
        max_series = int(style.get("maxSeries") or 12)
    except TypeError, ValueError:
        max_series = 12
    return {
        "colors": palette["colors"],
        "seq": palette["sequential"],
        "theme": theme,
        "tokens": THEME_COLORS[theme],
        "notes": [],
        "stats": [],
        "sampled": False,
        "max_points": int(limits.get("max_points", 10_000)),
        "max_series": max(2, min(max_series, 30)),
        "kinds": kinds,
    }


def auto_title(chart: Any, fields: dict[str, Any], options: dict[str, Any]) -> str:
    def name(value: Any) -> str:
        return (
            ", ".join(value[:3]) + ("…" if len(value) > 3 else "") if isinstance(value, list) else str(value)
        )

    x, y = fields.get("x"), fields.get("y")
    if chart.category in {"comparison", "composition", "trend"} and x:
        measure = d._value_label(name(y), options).strip() if y else "Count"
        return f"{measure} by {name(x)}" if chart.id not in {"candlestick", "range_band"} else chart.name
    if chart.category == "relationship" and x and y:
        return f"{name(y)} vs {name(x)}"
    if chart.category == "distribution":
        return f"Distribution of {name(fields.get('y') or x)}" + (
            f" by {name(x)}" if fields.get("y") and x else ""
        )
    if fields.get("actual"):
        return f"{chart.name}: {name(fields['actual'])}"
    if fields.get("target"):
        return f"{chart.name}: {name(fields['target'])}"
    return chart.name


def _fallback_insight(frame: Any, fields: dict[str, Any], ctx: dict[str, Any]) -> str:
    """A data-driven takeaway for charts without a specific one: typical value, range and leading group."""
    kinds = ctx["kinds"]
    chosen = []
    for key in ("y", "z", "x", "close", "size", "features", "path"):
        value = fields.get(key)
        chosen += value if isinstance(value, list) else ([value] if value else [])
    numeric = next((column for column in chosen if kinds.get(column) == "numeric"), None)
    parts = []
    if numeric:
        values = d._numeric(frame, numeric).dropna()
        if len(values):
            parts.append(
                f"{numeric} is typically {compact(float(values.median()))} (range "
                f"{compact(float(values.min()))} to {compact(float(values.max()))})"
            )
    group = fields.get("color") or next(
        (column for column in chosen if kinds.get(column) in {"categorical", "boolean"}), None
    )
    if isinstance(group, str) and group in frame.columns:
        counts = d._label(frame[group]).value_counts()
        if len(counts) > 1:
            if numeric:
                totals = (
                    frame.assign(_v=d._numeric(frame, numeric))
                    .groupby(d._label(frame[group]))["_v"]
                    .mean()
                    .sort_values(ascending=False)
                )
                parts.append(
                    f"{totals.index[0]} has the highest average {numeric} "
                    f"({compact(float(totals.iloc[0]))}) of {len(counts)} {group} groups"
                )
            else:
                parts.append(
                    f"{counts.index[0]} is the most common {group} ({counts.iloc[0] / counts.sum():.0%})"
                )
    if not parts:
        parts.append(f"The chart summarises {len(frame.index):,} rows")
    return "; ".join(parts) + "."


def explain(chart: Any, title: str, ctx: dict[str, Any]) -> dict[str, str]:
    """A plain-language explanation: what the chart shows, how to read it, and the key takeaway."""
    insight = ctx.get("insight") or ""
    if not insight and ctx["stats"]:
        insight = "; ".join(f"{s['label']}: {s['value']}" for s in ctx["stats"][:3]) + "."
    return {
        "about": (
            f"This {chart.name.lower()} shows "
            f"{title[0].lower() + title[1:] if title else chart.description.lower()}."
        ),
        "howToRead": catalog.READ_GUIDE.get(chart.id, chart.description),
        "insight": insight,
    }


def render(
    frame: Any, kinds: dict[str, str], request: dict[str, Any], limits: dict[str, Any]
) -> dict[str, Any]:
    import json

    chart = catalog.CHARTS_BY_ID.get(request.get("chart_type") or "")
    if chart is None:
        raise AnalysisError("Unknown chart type.")
    try:
        fields, options = catalog.validate_selection(
            chart.slots, chart.options, request.get("fields") or {}, request.get("options") or {}, kinds
        )
    except ValueError as exc:
        raise AnalysisError(str(exc)) from exc
    style = request.get("style") or {}
    ctx = context(style, limits, kinds)
    frame = d.typed(frame, kinds)
    if frame.empty:
        raise AnalysisError("The dataset has no rows to chart.")
    try:
        option = RENDERERS[chart.id](frame, fields, options, ctx)
    except (KeyError, ValueError, TypeError, ZeroDivisionError, IndexError) as exc:
        if isinstance(exc, AnalysisError):
            raise
        raise AnalysisError(
            f"This chart could not be drawn from the chosen columns ({type(exc).__name__}: {str(exc)[:200]})."
        ) from exc
    mapping = label_map(frame.columns)
    humanize_option(option, mapping)
    title = str(style.get("title") or "").strip()[:200] or humanize_text(
        auto_title(chart, fields, options), mapping, capital=True
    )
    if style.get("showLegend") is False:
        option["legend"] = {"show": False}
    for key, axis_key in (("xTitle", "xAxis"), ("yTitle", "yAxis")):
        if style.get(key):
            for axis in _each(option, axis_key)[:1]:
                axis["name"] = str(style[key])[:120]
    if not ctx.get("insight") and not ctx["stats"]:
        _insight(ctx, _fallback_insight(frame, fields, ctx))
    humanize_context(ctx, mapping)
    option = finish(option, ctx)
    encoded = json.dumps(option, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > limits["max_spec_bytes"]:
        raise AnalysisError("The chart is too large; filter the data or choose a summary chart.")
    return {
        "option": option,
        "renderer": "echarts",
        "gl": any(
            str(series.get("type", "")).endswith("3D") or series.get("type") == "surface"
            for series in option.get("series", [])
        ),
        "map": "world"
        if ("geo" in option or any(series.get("type") == "map" for series in option.get("series", [])))
        else None,
        "title": title,
        "chartType": chart.id,
        "fields": fields,
        "options": options,
        "notes": ctx["notes"],
        "stats": ctx["stats"],
        "table": option_table(option),
        "explanation": explain(chart, title, ctx),
        "rows": int(len(frame.index)),
        "sampled": ctx["sampled"],
    }
