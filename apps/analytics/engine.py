"""Isolated analytics engine (pandas, Plotly, Matplotlib).

This module deliberately imports nothing from Django or the application: the
analysis workers run it as a separate ``python -I`` process with an empty
environment (no credentials), a private temporary working directory and kernel
resource limits applied *before* any untrusted data is read. It reads one JSON
request from stdin and writes one JSON response to stdout.

The transform language is declarative and fixed; no expression, formula or
code from a request is ever evaluated.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import sys
from typing import Any

TRANSFORM_KEYS = {"select", "filter", "filters", "group_by", "aggregate", "sort", "limit"}
FILTER_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "in"}
AGGREGATES = {"count", "sum", "mean", "min", "max"}
CHART_KINDS = {"bar", "line", "scatter", "histogram", "box", "pie", "area"}


class AnalysisError(ValueError):
    """A safe, user-facing validation error."""


# --- Validation ----------------------------------------------------------------


def validate_transform_spec(transform: Any) -> dict[str, Any]:
    if transform in (None, {}):
        return {}
    if not isinstance(transform, dict):
        raise AnalysisError("transform must be an object.")
    unknown = set(transform) - TRANSFORM_KEYS
    if unknown:
        raise AnalysisError(f"Unsupported transform keys: {', '.join(sorted(unknown))}.")
    if "filter" in transform and "filters" in transform:
        raise AnalysisError("Use either filter or filters, not both.")
    if "select" in transform:
        select = transform["select"]
        if not isinstance(select, list) or not select or len(select) > 200:
            raise AnalysisError("select must be a non-empty list of at most 200 columns.")
        if not all(isinstance(item, str) and item for item in select) or len(set(select)) != len(select):
            raise AnalysisError("select columns must be unique, non-empty strings.")
    filters = transform.get("filters", transform.get("filter")) or []
    if isinstance(filters, dict):
        filters = [filters]
    if not isinstance(filters, list) or len(filters) > 20:
        raise AnalysisError("filters must be a list containing at most 20 conditions.")
    for condition in filters:
        if not isinstance(condition, dict):
            raise AnalysisError("Each filter must be an object.")
        if set(condition) == {"column", "equals"}:
            if not isinstance(condition["column"], str) or not condition["column"]:
                raise AnalysisError("Filter columns must be non-empty strings.")
            continue
        if set(condition) != {"column", "operator", "value"}:
            raise AnalysisError("Filters require column, operator, and value.")
        if condition["operator"] not in FILTER_OPERATORS:
            raise AnalysisError("Unsupported filter operator.")
        if not isinstance(condition["column"], str) or not condition["column"]:
            raise AnalysisError("Filter columns must be non-empty strings.")
        if condition["operator"] == "in" and (
            not isinstance(condition["value"], list) or len(condition["value"]) > 100
        ):
            raise AnalysisError("The in operator requires a list of at most 100 values.")
    group_by = transform.get("group_by")
    if group_by is not None:
        groups = [group_by] if isinstance(group_by, str) else group_by
        if not isinstance(groups, list) or not groups or len(groups) > 5:
            raise AnalysisError("group_by must name between 1 and 5 columns.")
        if not all(isinstance(item, str) and item for item in groups):
            raise AnalysisError("group_by columns must be non-empty strings.")
        if len(groups) != len(set(groups)):
            raise AnalysisError("group_by columns must be unique.")
        aggregate = transform.get("aggregate")
        if not isinstance(aggregate, (str, dict)) or not aggregate:
            raise AnalysisError("group_by requires an aggregate.")
        if isinstance(aggregate, dict) and not all(
            isinstance(column, str) and column and isinstance(operation, str)
            for column, operation in aggregate.items()
        ):
            raise AnalysisError("aggregate must map column names to aggregate operations.")
        operations = {aggregate} if isinstance(aggregate, str) else set(aggregate.values())
        if not operations <= AGGREGATES:
            raise AnalysisError("aggregate operations must be count, sum, mean, min, or max.")
        if isinstance(aggregate, dict) and len(aggregate) > 20:
            raise AnalysisError("aggregate may contain at most 20 columns.")
    elif "aggregate" in transform:
        raise AnalysisError("aggregate requires group_by.")
    sort = transform.get("sort")
    if sort is not None:
        sort_items = [sort] if isinstance(sort, dict) else sort
        if not isinstance(sort_items, list) or not sort_items or len(sort_items) > 10:
            raise AnalysisError("sort must contain between 1 and 10 rules.")
        for rule in sort_items:
            if (
                not isinstance(rule, dict)
                or set(rule) != {"column", "direction"}
                or rule["direction"] not in {"asc", "desc"}
            ):
                raise AnalysisError("Each sort rule requires column and asc or desc direction.")
            if not isinstance(rule["column"], str) or not rule["column"]:
                raise AnalysisError("Sort columns must be non-empty strings.")
    if "limit" in transform:
        limit = transform["limit"]
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise AnalysisError("limit must be between 1 and 10000.")
    return transform


# --- Data ------------------------------------------------------------------------


def dataframe_from_bytes(content: bytes, *, mime_type: str, limits: dict[str, Any]) -> Any:
    if not content:
        raise AnalysisError("The dataset is empty.")
    if len(content) > limits["max_bytes"]:
        raise AnalysisError("The dataset exceeds the configured byte limit.")
    normalized = mime_type.split(";", 1)[0].strip().lower()
    if normalized not in limits["allowed_mime_types"]:
        raise AnalysisError("Only CSV datasets are supported.")
    if b"\x00" in content:
        raise AnalysisError("CSV datasets cannot contain NUL bytes.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AnalysisError("CSV datasets must use UTF-8 encoding.") from exc
    try:
        header = next(csv.reader(io.StringIO(text)), [])
    except csv.Error as exc:
        raise AnalysisError("The CSV header is invalid.") from exc
    if not header or any(not column.strip() for column in header):
        raise AnalysisError("CSV datasets require non-empty column names.")
    if len(header) != len(set(header)):
        raise AnalysisError("CSV column names must be unique.")
    if len(header) > limits["max_columns"]:
        raise AnalysisError(f"Datasets may contain at most {limits['max_columns']} columns.")
    import pandas as pd

    try:
        frame = pd.read_csv(io.StringIO(text), nrows=limits["max_rows"] + 1, on_bad_lines="error")
    except (ValueError, TypeError, pd.errors.ParserError) as exc:
        raise AnalysisError("The CSV dataset could not be parsed.") from exc
    if len(frame.index) > limits["max_rows"]:
        raise AnalysisError(f"Datasets may contain at most {limits['max_rows']} rows.")
    if len(frame.index) * len(frame.columns) > limits["max_cells"]:
        raise AnalysisError(f"Datasets may contain at most {limits['max_cells']} cells.")
    return frame


def _require_columns(frame: Any, columns: list[str], operation: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise AnalysisError(f"{operation} names unknown columns: {', '.join(missing)}.")


def apply_transform(frame: Any, transform: dict[str, Any]) -> Any:
    """Apply the fixed, validated transform language (no dynamic evaluation)."""
    transform = validate_transform_spec(transform)
    filters = transform.get("filters", transform.get("filter", []))
    if isinstance(filters, dict):
        filters = [filters]
    for condition in filters:
        column = condition["column"]
        _require_columns(frame, [column], "filter")
        operator = condition.get("operator", "eq")
        value = condition.get("value", condition.get("equals"))
        series = frame[column]
        try:
            if operator == "eq":
                mask = series == value
            elif operator == "ne":
                mask = series != value
            elif operator == "gt":
                mask = series > value
            elif operator == "gte":
                mask = series >= value
            elif operator == "lt":
                mask = series < value
            elif operator == "lte":
                mask = series <= value
            elif operator == "contains":
                mask = series.astype("string").str.contains(str(value), regex=False, na=False)
            else:
                mask = series.isin(value)
        except (TypeError, ValueError) as exc:
            raise AnalysisError(f"Filter value is incompatible with column {column}.") from exc
        frame = frame[mask]

    group_by = transform.get("group_by")
    if group_by:
        groups = [group_by] if isinstance(group_by, str) else group_by
        _require_columns(frame, groups, "group_by")
        aggregate = transform["aggregate"]
        if aggregate == "count":
            frame = frame.groupby(groups, dropna=False).size().reset_index(name="count")
        elif isinstance(aggregate, str):
            numeric = [
                column for column in frame.select_dtypes(include="number").columns if column not in groups
            ]
            if not numeric:
                raise AnalysisError("The requested aggregate requires numeric columns.")
            frame = frame.groupby(groups, dropna=False)[numeric].agg(aggregate).reset_index()
        else:
            _require_columns(frame, list(aggregate), "aggregate")
            named = {}
            for column, operation in aggregate.items():
                if operation != "count" and column not in frame.select_dtypes(include="number").columns:
                    raise AnalysisError(f"Aggregate {operation} requires numeric column {column}.")
                named[f"{column}_{operation}"] = (column, operation)
            frame = frame.groupby(groups, dropna=False).agg(**named).reset_index()

    if columns := transform.get("select"):
        _require_columns(frame, columns, "select")
        frame = frame[columns]
    sort = transform.get("sort")
    if sort:
        rules = [sort] if isinstance(sort, dict) else sort
        columns = [rule["column"] for rule in rules]
        _require_columns(frame, columns, "sort")
        frame = frame.sort_values(
            columns, ascending=[rule["direction"] == "asc" for rule in rules], kind="mergesort"
        )
    if limit := transform.get("limit"):
        frame = frame.head(limit)
    return frame.reset_index(drop=True)


def _json_value(value: Any) -> Any:
    try:
        import math

        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        if hasattr(value, "item"):
            value = value.item()
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return value if isinstance(value, (int, float, str, bool)) or value is None else str(value)
    except TypeError, ValueError:
        return str(value)


def profile_frame(frame: Any) -> dict[str, Any]:
    """Per-column profile: types, completeness, cardinality, statistics and top values."""
    import pandas as pd

    rows = int(len(frame.index))
    columns: list[dict[str, Any]] = []
    for name in frame.columns:
        series = frame[name]
        nulls = int(series.isna().sum())
        entry: dict[str, Any] = {
            "name": str(name),
            "dtype": str(series.dtype),
            "nulls": nulls,
            "nullRatio": round(nulls / rows, 6) if rows else 0.0,
            "distinct": int(series.nunique(dropna=True)),
        }
        non_null = series.dropna()
        if pd.api.types.is_bool_dtype(series):
            entry["kind"] = "boolean"
            entry["trueCount"] = int(non_null.sum())
        elif pd.api.types.is_numeric_dtype(series):
            entry["kind"] = "numeric"
            if not non_null.empty:
                quantiles = non_null.quantile([0.25, 0.5, 0.75])
                entry.update(
                    {
                        "min": _json_value(non_null.min()),
                        "max": _json_value(non_null.max()),
                        "mean": _json_value(round(float(non_null.mean()), 6)),
                        "std": _json_value(round(float(non_null.std()), 6)) if len(non_null) > 1 else None,
                        "p25": _json_value(quantiles.loc[0.25]),
                        "median": _json_value(quantiles.loc[0.5]),
                        "p75": _json_value(quantiles.loc[0.75]),
                    }
                )
        else:
            parsed = (
                pd.to_datetime(non_null, errors="coerce", format="mixed") if not non_null.empty else non_null
            )
            if not non_null.empty and parsed.notna().mean() >= 0.95:
                entry["kind"] = "datetime"
                entry["min"] = _json_value(parsed.min())
                entry["max"] = _json_value(parsed.max())
            else:
                entry["kind"] = "categorical"
                counts = non_null.astype(str).value_counts().head(5)
                entry["topValues"] = [
                    {"value": str(key), "count": int(count)} for key, count in counts.items()
                ]
        columns.append(entry)
    return {
        "rows": rows,
        "column_count": int(len(frame.columns)),
        "columns": [str(column) for column in frame.columns],
        "dtypes": {str(column): str(dtype) for column, dtype in frame.dtypes.items()},
        "nulls": {str(column): int(value) for column, value in frame.isna().sum().items()},
        "columnProfiles": columns,
    }


def frame_preview(frame: Any, rows: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = json.loads(frame.head(rows).to_json(orient="records", date_format="iso"))
    return records


def chart(
    frame: Any, *, kind: str, x: str, y: str, title: str = "", color: str = "", max_points: int
) -> tuple[dict[str, Any], bytes]:
    if kind not in CHART_KINDS:
        raise AnalysisError("Unsupported chart type.")
    if len(frame.index) > max_points:
        raise AnalysisError(f"Charts may contain at most {max_points} points; add a limit.")
    required = [x] if kind == "histogram" else [x, y]
    if color:
        required.append(color)
    _require_columns(frame, required, "chart")
    if frame.empty:
        raise AnalysisError("A chart cannot be created from an empty result.")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd
    import plotly.express as px

    numeric_column = x if kind == "histogram" else y
    if not pd.api.types.is_numeric_dtype(frame[numeric_column]):
        raise AnalysisError(f"Chart column {numeric_column} must be numeric.")
    options: dict[str, Any] = {"title": title or None}
    if color:
        options["color"] = color
    builders = {
        "bar": lambda: px.bar(frame, x=x, y=y, **options),
        "line": lambda: px.line(frame, x=x, y=y, **options),
        "area": lambda: px.area(frame, x=x, y=y, **options),
        "scatter": lambda: px.scatter(frame, x=x, y=y, **options),
        "histogram": lambda: px.histogram(frame, x=x, **options),
        "box": lambda: px.box(frame, x=x, y=y, **options),
        "pie": lambda: px.pie(frame, names=x, values=y, title=title or None),
    }
    plotly_spec = json.loads(builders[kind]().to_json())

    figure, axis = plt.subplots(figsize=(10, 6))
    if kind == "bar":
        axis.bar(frame[x].astype(str), frame[y])
    elif kind == "line":
        axis.plot(frame[x], frame[y])
    elif kind == "area":
        axis.fill_between(range(len(frame.index)), frame[y])
        axis.set_xticks(range(len(frame.index)), frame[x].astype(str), rotation=45, ha="right")
    elif kind == "scatter":
        axis.scatter(frame[x], frame[y])
    elif kind == "box":
        groups = [(str(key), group[y].dropna()) for key, group in frame.groupby(x)]
        axis.boxplot([values for _, values in groups], tick_labels=[label for label, _ in groups])
    elif kind == "pie":
        axis.pie(frame[y], labels=frame[x].astype(str), autopct="%1.1f%%")
        axis.axis("equal")
    else:
        axis.hist(frame[x].dropna())
    if kind != "pie":
        axis.set_xlabel(x)
        axis.set_ylabel(y or "count")
    if title:
        axis.set_title(title)
    output = io.BytesIO()
    figure.savefig(output, format="png", bbox_inches="tight", dpi=120)
    plt.close(figure)
    return plotly_spec, output.getvalue()


# --- Process entry point -----------------------------------------------------------


def _apply_resource_limits(limits: dict[str, Any]) -> None:
    import resource

    memory = int(limits["memory_bytes"])
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    cpu = int(limits["cpu_seconds"])
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    file_bytes = int(limits["file_bytes"])
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def handle(request: dict[str, Any]) -> dict[str, Any]:
    operation = request["op"]
    limits = request["limits"]
    content = base64.b64decode(request["data"])
    frame = dataframe_from_bytes(content, mime_type=request.get("mime_type", "text/csv"), limits=limits)
    if operation == "profile":
        return {"profile": profile_frame(frame)}
    if operation == "analyze":
        source_profile = profile_frame(frame)
        result = apply_transform(frame, request.get("transform") or {})
        result_bytes = result.to_csv(index=False).encode("utf-8")
        if not result_bytes or len(result_bytes) > limits["max_result_bytes"]:
            raise AnalysisError("The analysis result exceeds the configured artifact limit.")
        return {
            "sourceProfile": source_profile,
            "profile": profile_frame(result),
            "preview": frame_preview(result, int(limits["preview_rows"])),
            "result": base64.b64encode(result_bytes).decode("ascii"),
        }
    if operation == "chart":
        spec, png = chart(
            frame,
            kind=request["kind"],
            x=request["x"],
            y=request.get("y") or "",
            title=request.get("title") or "",
            color=request.get("color") or "",
            max_points=int(limits["max_points"]),
        )
        spec_json = json.dumps(spec, separators=(",", ":"))
        if len(spec_json.encode("utf-8")) > limits["max_spec_bytes"]:
            raise AnalysisError("The interactive chart specification exceeds the configured limit.")
        return {
            "spec": spec_json,
            "png": base64.b64encode(png).decode("ascii"),
            "points": int(len(frame.index)),
        }
    raise AnalysisError(f"Unsupported operation {operation!r}.")


def main() -> int:
    limits = json.loads(os.environ.get("JT_ANALYTICS_LIMITS", "{}"))
    _apply_resource_limits(limits)
    try:
        request = json.loads(sys.stdin.buffer.read())
        response: dict[str, Any] = {"ok": True, **handle(request)}
    except AnalysisError as exc:
        response = {"ok": False, "kind": "analysis", "error": str(exc)[:2000]}
    except MemoryError:
        response = {"ok": False, "kind": "resources", "error": "The analysis exceeded its memory limit."}
    except Exception as exc:  # noqa: BLE001 - reported to the parent without a traceback
        response = {"ok": False, "kind": "internal", "error": type(exc).__name__}
    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
