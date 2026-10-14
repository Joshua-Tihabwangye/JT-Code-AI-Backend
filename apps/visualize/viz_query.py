"""Dashboard queries with pandas (engine side) and the result post-processing every visual uses.

``aggregate`` runs a query from ``visual_plan`` on a data frame with exactly the semantics of the
SQL path in ``store.py`` (nulls are their own group, ``sum`` of nothing is null, ``count(field)``
skips blanks, ``first``/``last`` skip blanks, ``ne``/``not_in`` exclude blanks), so a dashboard
looks the same whichever path answered. The helpers below turn a result into a frame and apply
“show value as”, Top N + Others and sorting.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

GRAIN_PERIOD = {"year": "Y", "quarter": "Q", "month": "M"}
# Grouped pandas methods per aggregation (first/last skip blanks, like the SQL path).
GROUPED = {"avg": "mean", "distinct": "nunique"}
BLANK = "(Blank)"


def _truncate(series: Any, grain: str) -> Any:
    values = pd.to_datetime(series, errors="coerce")
    if grain in GRAIN_PERIOD:
        return values.dt.to_period(GRAIN_PERIOD[grain]).dt.start_time
    if grain == "week":
        days = values.dt.normalize()
        return days - pd.to_timedelta(days.dt.weekday, unit="D")
    return values.dt.normalize()


def _param(value: Any, kind: str) -> Any:
    if kind == "datetime" and value is not None:
        return pd.Timestamp(value)
    return value


def apply_filters(frame: Any, filters: list[dict[str, Any]], kinds: dict[str, str]) -> Any:
    mask = pd.Series(True, index=frame.index)
    for item in filters:
        column = frame[item["field"]]
        kind = kinds.get(item["field"], "categorical")
        if kind == "datetime":
            column = pd.to_datetime(column, errors="coerce")
        op, value = item["op"], item.get("value")
        if op == "blank":
            mask &= column.isna()
        elif op == "not_blank":
            mask &= column.notna()
        elif op in {"in", "not_in"}:
            values = [_param(v, kind) for v in value if v is not None]
            hit = column.isin(values) | (column.isna() if None in value else False)
            mask &= hit if op == "in" else (column.notna() & ~hit)
        elif op == "between":
            low, high = (_param(v, kind) for v in value)
            mask &= column.notna() & (column >= low) & (column <= high)
        elif op in {"contains", "starts_with", "ends_with"}:
            text = column.astype("string").str.lower()
            needle = str(value).lower()
            hit = (
                text.str.contains(needle, regex=False)
                if op == "contains"
                else text.str.startswith(needle)
                if op == "starts_with"
                else text.str.endswith(needle)
            )
            mask &= hit.fillna(False).astype(bool)
        else:
            param = _param(value, kind)
            comparisons = {
                "eq": column == param,
                "ne": column.notna() & (column != param),
                "gt": column > param,
                "gte": column >= param,
                "lt": column < param,
                "lte": column <= param,
            }
            mask &= comparisons[op].fillna(False).astype(bool)
    return frame[mask]


def _measure(series: Any, agg: str) -> Any:
    if agg == "sum":
        return series.sum(min_count=1)
    if agg == "avg":
        return series.mean()
    if agg == "count":
        return series.count()
    if agg == "distinct":
        return series.nunique()
    if agg == "median":
        return series.median()
    if agg == "std":
        return series.std(ddof=1)
    if agg == "first":
        values = series.dropna()
        return values.iloc[0] if len(values) else None
    if agg == "last":
        values = series.dropna()
        return values.iloc[-1] if len(values) else None
    return getattr(series, agg)()


def _plain(value: Any) -> Any:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
        if isinstance(value, float) and np.isnan(value):
            return None
    if value is pd.NaT:
        return None
    return value


def raw_rows(frame: Any, query: dict[str, Any], kinds: dict[str, str]) -> dict[str, Any]:
    """Filtered rows of a few columns, evenly sampled to the limit (same rule as the SQL path)."""
    # Rows are numbered by their position in the whole dataset (like ``_row`` in Postgres), so both
    # paths keep exactly the same rows.
    step = max(1, -(-len(frame.index) // query["limit"]))
    data = apply_filters(frame, query["filters"], kinds)[query["columns"]]
    positions = pd.Series(range(len(frame.index)), index=frame.index).loc[data.index]
    sample = data[(positions % step) == 0].head(query["limit"])
    return {
        "columns": list(query["columns"]),
        "rows": [[_plain(value) for value in record] for record in sample.itertuples(index=False)],
        "truncated": step > 1,
    }


def aggregate(frame: Any, query: dict[str, Any], kinds: dict[str, str]) -> dict[str, Any]:
    if query.get("rows"):
        return raw_rows(frame, query, kinds)
    data = apply_filters(frame, query["filters"], kinds)
    groups = query["groupBy"]
    measures = query["measures"]
    columns = [f"g{i}" for i in range(len(groups))] + [m["id"] for m in measures]
    work = pd.DataFrame(index=data.index)
    for index, group in enumerate(groups):
        column = data[group["field"]]
        work[f"g{index}"] = _truncate(column, group["grain"]) if group.get("grain") else column
    for measure in measures:
        if measure["field"] == "*":
            work[measure["id"]] = 1
        else:
            column = data[measure["field"]]
            work[measure["id"]] = (
                pd.to_datetime(column, errors="coerce")
                if kinds.get(measure["field"]) == "datetime"
                else column
            )
    if not groups:
        row = [
            (int(len(work.index)) if m["field"] == "*" else _plain(_measure(work[m["id"]], m["agg"])))
            for m in measures
        ]
        return {"columns": columns, "rows": [row], "truncated": False}
    keys = [f"g{i}" for i in range(len(groups))]
    grouped = work.groupby(keys, dropna=False, sort=True)
    out = grouped.size().rename("__size").to_frame()
    for measure in measures:
        if measure["field"] == "*":
            out[measure["id"]] = out["__size"]
        else:
            column = grouped[measure["id"]]
            agg = measure["agg"]
            out[measure["id"]] = (
                column.sum(min_count=1)
                if agg == "sum"
                else column.std(ddof=1)
                if agg == "std"
                else getattr(column, GROUPED.get(agg, agg))()
            )
    out = out.drop(columns="__size").reset_index()
    truncated = len(out.index) > query["limit"]
    rows = [
        [_plain(value) for value in record] for record in out.head(query["limit"]).itertuples(index=False)
    ]
    return {"columns": columns, "rows": rows, "truncated": truncated}


# --- Post-processing shared by every visual ------------------------------------------------------


def frame(result: dict[str, Any]) -> Any:
    """A result as a data frame (dates stay ISO text; blanks become “(Blank)” in category columns)."""
    table = pd.DataFrame(result.get("rows") or [], columns=result.get("columns") or [])
    for column in table.columns:
        if column.startswith("g"):
            table[column] = table[column].map(lambda v: BLANK if v is None else v)
        else:
            table[column] = pd.to_numeric(table[column], errors="coerce")
    return table


def show_value_as(
    table: Any, value: str, mode: str, row: str | None = None, column: str | None = None
) -> Any:
    series = table[value].astype(float)
    if mode == "pct_total":
        total = series.sum()
        return series / total if total else series * np.nan
    if mode == "pct_row" and row:
        return series / table.groupby(row)[value].transform("sum").replace(0, np.nan)
    if mode == "pct_column" and column:
        return series / table.groupby(column)[value].transform("sum").replace(0, np.nan)
    if mode == "running_total":
        return series.cumsum()
    if mode == "diff_prev":
        return series.diff()
    if mode == "pct_diff_prev":
        previous = series.shift(1)
        return (series - previous) / previous.abs().replace(0, np.nan)
    if mode == "rank":
        return series.rank(ascending=False, method="min")
    return series


def top_n(table: Any, category: str, value: str, n: int, others: bool, additive: bool) -> tuple[Any, bool]:
    """Keep the ``n`` largest categories; fold the rest into “Others” when the measure adds up."""
    if n <= 0 or table[category].nunique() <= n:
        return table, False
    totals = table.groupby(category)[value].sum().sort_values(ascending=False)
    keep = set(totals.index[:n])
    kept = table[table[category].isin(keep)]
    if not others or not additive:
        return kept, True
    rest = table[~table[category].isin(keep)].copy()
    rest[category] = "Others"
    numeric = [c for c in rest.columns if not c.startswith("g")]
    keys = [c for c in rest.columns if c.startswith("g")]
    folded = rest.groupby(keys, dropna=False, sort=False)[numeric].sum(min_count=1).reset_index()
    return pd.concat([kept, folded], ignore_index=True), True
