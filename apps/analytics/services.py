"""Bounded, declarative analytics. Uploaded code is never executed."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings


class AnalysisError(ValueError):
    """A safe validation error that may be persisted on an analysis run."""


def _limits() -> tuple[int, int, int]:
    return (
        settings.ANALYTICS_MAX_DATASET_ROWS,
        settings.ANALYTICS_MAX_DATASET_COLUMNS,
        settings.ANALYTICS_MAX_DATASET_CELLS,
    )


def validate_transform_spec(transform: Any) -> dict[str, Any]:
    if transform in (None, {}):
        return {}
    if not isinstance(transform, dict):
        raise AnalysisError("transform must be an object.")
    allowed = {"select", "filter", "filters", "group_by", "aggregate", "sort", "limit"}
    unknown = set(transform) - allowed
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
    filters = transform.get("filters", transform.get("filter", []))
    if isinstance(filters, dict):
        filters = [filters]
    if filters and (not isinstance(filters, list) or len(filters) > 20):
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
        if condition["operator"] not in {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "in"}:
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
        if not operations <= {"count", "sum", "mean", "min", "max"}:
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


def dataframe_from_bytes(content: bytes, *, mime_type: str):
    if not content:
        raise AnalysisError("The dataset is empty.")
    if len(content) > settings.ANALYTICS_MAX_DATASET_BYTES:
        raise AnalysisError("The dataset exceeds the configured byte limit.")
    normalized = mime_type.split(";", 1)[0].strip().lower()
    if normalized not in settings.ANALYTICS_ALLOWED_MIME_TYPES:
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
    max_rows, max_columns, max_cells = _limits()
    if len(header) > max_columns:
        raise AnalysisError(f"Datasets may contain at most {max_columns} columns.")
    import pandas as pd

    try:
        frame = pd.read_csv(io.StringIO(text), nrows=max_rows + 1, on_bad_lines="error")
    except (ValueError, TypeError, pd.errors.ParserError) as exc:
        raise AnalysisError("The CSV dataset could not be parsed.") from exc
    if len(frame.index) > max_rows:
        raise AnalysisError(f"Datasets may contain at most {max_rows} rows.")
    if len(frame.index) * len(frame.columns) > max_cells:
        raise AnalysisError(f"Datasets may contain at most {max_cells} cells.")
    return frame


def bytes_for_asset(asset) -> bytes:
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
    return response.content


def dataframe_for_dataset(dataset):
    if dataset.inline_data:
        content = dataset.inline_data.encode("utf-8")
    elif dataset.asset_id:
        content = bytes_for_asset(dataset.asset)
    else:
        raise AnalysisError("The dataset has no source data.")
    return dataframe_from_bytes(content, mime_type=dataset.mime_type)


def dataframe_for_result(run):
    if not run.result_asset_id:
        raise AnalysisError("The analysis result artifact is unavailable.")
    content = bytes_for_asset(run.result_asset)
    return dataframe_from_bytes(content, mime_type="text/csv")


def _require_columns(frame, columns: list[str], operation: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise AnalysisError(f"{operation} names unknown columns: {', '.join(missing)}.")


def apply_transform(frame, transform: dict[str, Any]):
    """Apply a fixed, validated transform language with no dynamic evaluation."""
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
            columns,
            ascending=[rule["direction"] == "asc" for rule in rules],
            kind="mergesort",
        )
    if limit := transform.get("limit"):
        frame = frame.head(limit)
    return frame.reset_index(drop=True)


def profile_frame(frame) -> dict[str, Any]:
    return {
        "rows": int(len(frame.index)),
        "column_count": int(len(frame.columns)),
        "columns": [str(column) for column in frame.columns],
        "dtypes": {str(column): str(dtype) for column, dtype in frame.dtypes.items()},
        "nulls": {str(column): int(value) for column, value in frame.isna().sum().items()},
    }


def frame_preview(frame) -> list[dict[str, Any]]:
    return json.loads(
        frame.head(settings.ANALYTICS_RESULT_PREVIEW_ROWS).to_json(orient="records", date_format="iso")
    )


def frame_as_csv(frame) -> bytes:
    return frame.to_csv(index=False).encode("utf-8")


def chart(frame, *, kind: str, x: str, y: str) -> tuple[dict[str, Any], bytes]:
    if len(frame.index) > settings.ANALYTICS_MAX_CHART_POINTS:
        raise AnalysisError(
            f"Charts may contain at most {settings.ANALYTICS_MAX_CHART_POINTS} points; add a limit."
        )
    required = [x] if kind == "histogram" else [x, y]
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
    if kind == "bar":
        figure = px.bar(frame, x=x, y=y)
    elif kind == "line":
        figure = px.line(frame, x=x, y=y)
    elif kind == "scatter":
        figure = px.scatter(frame, x=x, y=y)
    elif kind == "histogram":
        figure = px.histogram(frame, x=x)
    else:
        raise AnalysisError("Unsupported chart type.")
    plotly_spec = json.loads(figure.to_json())
    fig, axis = plt.subplots(figsize=(10, 6))
    if kind == "bar":
        axis.bar(frame[x].astype(str), frame[y])
    elif kind == "line":
        axis.plot(frame[x], frame[y])
    elif kind == "scatter":
        axis.scatter(frame[x], frame[y])
    else:
        axis.hist(frame[x].dropna())
    axis.set_xlabel(x)
    axis.set_ylabel(y or "count")
    output = io.BytesIO()
    fig.savefig(output, format="png", bbox_inches="tight", dpi=120)
    plt.close(fig)
    return plotly_spec, output.getvalue()


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
