"""Applied steps: an ordered, validated pipeline of data transforms (Power Query style).

A pipeline is a list of steps ``{"op": ..., settings..., "enabled": true, "label": "..."}``. The
whole pipeline is validated before anything runs (``validate``), then applied in order
(``run``); a preview can stop after any step (``upto``). Disabled steps are skipped. Nothing here
evaluates user text as code: formulas use the safe expression language (``viz_expr.row_tree``)
and conditions use the dashboard filter operators (``viz_query._mask``).

This module imports nothing from Django; the sandboxed engine loads it by path.
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent


def _sibling(name: str, filename: str) -> Any:
    if name not in sys.modules:
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, _HERE / filename)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


_q = _sibling("jt_viz_query", "viz_query.py")
_expr = _sibling("jt_viz_expr", "viz_expr.py")

MAX_STEPS = 60
MAX_OTHERS = 3
FILTER_OPS = {
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
}
AGGS = {"sum", "avg", "count", "distinct", "min", "max", "median", "first", "last"}
DATE_PARTS = {"year", "quarter", "month", "month_name", "week", "weekday", "weekday_name", "day", "hour"}

# op → (label, settings keys that must be present)
OPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "remove_duplicates": ("Remove duplicate rows", ()),
    "remove_blank_rows": ("Remove blank rows", ()),
    "remove_columns": ("Remove columns", ("columns",)),
    "keep_columns": ("Keep only these columns", ("columns",)),
    "keep_rows": ("Keep top or bottom rows", ("n",)),
    "rename": ("Rename a column", ("column", "name")),
    "change_type": ("Change type", ("column", "type")),
    "trim": ("Trim spaces", ()),
    "text_case": ("Change text case", ("column", "case")),
    "replace_values": ("Replace values", ("column", "find")),
    "fill_blanks": ("Fill blanks", ("column", "strategy")),
    "filter_rows": ("Filter rows", ("conditions",)),
    "sort": ("Sort rows", ("by",)),
    "split_column": ("Split a column", ("column", "delimiter")),
    "merge_columns": ("Merge columns", ("columns", "name")),
    "date_parts": ("Extract date parts", ("column", "parts")),
    "conditional_column": ("Conditional column", ("name", "rules")),
    "calculated_column": ("Calculated column", ("name", "expression")),
    "bins": ("Group numbers into bins", ("column",)),
    "group_by": ("Group by", ("by", "aggregations")),
    "pivot": ("Pivot", ("index", "columns", "values")),
    "unpivot": ("Unpivot", ("keep",)),
    "merge": ("Merge with another dataset", ("dataset", "left_on", "right_on")),
    "append": ("Append another dataset", ("dataset",)),
    "outliers": ("Handle outliers", ("column", "action")),
    "date_table": ("Date table", ("column",)),
}


class StepError(ValueError):
    """A step is malformed or cannot run on this data; the message names the step."""


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise StepError(f"{label} must be a name (1–120 characters).")
    return value.strip()


def _names(value: Any, label: str, limit: int = 200) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > limit:
        raise StepError(f"{label} must list 1–{limit} columns.")
    return [_name(item, label) for item in value]


def validate(pipeline: Any) -> list[dict[str, Any]]:
    """The pipeline with defaults filled in; raises ``StepError`` for anything malformed."""
    if not isinstance(pipeline, list):
        raise StepError("Applied steps must be a list.")
    if len(pipeline) > MAX_STEPS:
        raise StepError(f"At most {MAX_STEPS} steps.")
    clean: list[dict[str, Any]] = []
    others: set[str] = set()
    for index, raw in enumerate(pipeline):
        where = f"Step {index + 1}"
        if not isinstance(raw, dict) or raw.get("op") not in OPS:
            raise StepError(f"{where}: unknown step {raw.get('op') if isinstance(raw, dict) else raw!r}.")
        label, required = OPS[raw["op"]]
        missing = [key for key in required if raw.get(key) in (None, "", [])]
        if missing:
            raise StepError(f"{where} ({label}): set {', '.join(missing)}.")
        step = {key: value for key, value in raw.items() if key not in {"enabled", "label"}}
        step["enabled"] = raw.get("enabled", True) is not False
        step["label"] = str(raw.get("label") or label)[:80]
        if raw["op"] in {"merge", "append"}:
            others.add(str(raw["dataset"]))
        if raw["op"] == "calculated_column":
            try:
                _expr.parse(raw["expression"])
            except _expr.ExprError as exc:
                raise StepError(f"{where} ({label}): {exc}") from exc
        if raw["op"] in {"filter_rows", "conditional_column"}:
            items = raw["conditions"] if raw["op"] == "filter_rows" else raw["rules"]
            if not isinstance(items, list) or not 1 <= len(items) <= 20:
                raise StepError(f"{where} ({label}): add 1–20 conditions.")
            for item in items:
                if not isinstance(item, dict) or item.get("op", "eq") not in FILTER_OPS:
                    raise StepError(f"{where} ({label}): unknown condition.")
        clean.append(step)
    if len(others) > MAX_OTHERS:
        raise StepError(f"Merge and append may use at most {MAX_OTHERS} other datasets.")
    return clean


def other_datasets(pipeline: list[dict[str, Any]]) -> list[str]:
    """The other datasets a pipeline merges or appends (the API loads them for the engine)."""
    return list(dict.fromkeys(str(s["dataset"]) for s in pipeline if s.get("op") in {"merge", "append"}))


def _kinds(frame: Any) -> dict[str, str]:
    import pandas as pd

    kinds = {}
    for column in frame.columns:
        series = frame[column]
        if pd.api.types.is_bool_dtype(series):
            kinds[column] = "boolean"
        elif pd.api.types.is_numeric_dtype(series):
            kinds[column] = "numeric"
        elif pd.api.types.is_datetime64_any_dtype(series):
            kinds[column] = "datetime"
        else:
            kinds[column] = "categorical"
    return kinds


def _column(frame: Any, name: Any, where: str) -> str:
    if name not in frame.columns:
        raise StepError(f"{where}: there is no column {name!r} at this step.")
    return str(name)


def _condition(item: dict[str, Any], frame: Any, where: str) -> dict[str, Any]:
    _column(frame, item.get("field"), where)
    kind = _kinds(frame)[item["field"]]
    op = item.get("op") or "eq"
    value = item.get("value")

    def typed(v: Any) -> Any:
        if v is None:
            return None
        if kind == "numeric":
            try:
                return float(v)
            except (TypeError, ValueError) as exc:
                raise StepError(f"{where}: {v!r} is not a number.") from exc
        if kind == "datetime":
            return str(v)[:19]
        return str(v)

    if op in {"in", "not_in"}:
        values = value if isinstance(value, list) else [value]
        return {"field": item["field"], "op": op, "value": [typed(v) for v in values[:500]]}
    if op == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise StepError(f"{where}: “between” needs two values.")
        return {"field": item["field"], "op": op, "value": [typed(value[0]), typed(value[1])]}
    if op in {"contains", "starts_with", "ends_with"}:
        return {"field": item["field"], "op": op, "value": str(value or "")[:200]}
    if op in {"blank", "not_blank"}:
        return {"field": item["field"], "op": op}
    return {"field": item["field"], "op": op, "value": typed(value)}


def _mask(frame: Any, items: list[dict[str, Any]], where: str, combine: str = "and") -> Any:
    import pandas as pd

    kinds = _kinds(frame)
    mask = pd.Series(combine == "and", index=frame.index)
    for item in items:
        part = _q._mask(frame, _condition(item, frame, where), kinds)
        mask = (mask & part) if combine == "and" else (mask | part)
    return mask


def _fresh(frame: Any, name: str, where: str) -> str:
    name = _name(name, where)
    if name in frame.columns:
        raise StepError(f"{where}: a column called {name!r} already exists.")
    return name


def _apply(step: dict[str, Any], frame: Any, others: dict[str, Any], limits: dict[str, Any]) -> Any:
    import numpy as np
    import pandas as pd

    op = step["op"]
    where = step["label"]
    if op == "remove_duplicates":
        subset = [_column(frame, c, where) for c in step.get("columns") or []] or None
        return frame.drop_duplicates(subset=subset)
    if op == "remove_blank_rows":
        subset = [_column(frame, c, where) for c in step.get("columns") or []] or None
        blanks = frame.replace(r"^\s*$", np.nan, regex=True)
        keep = blanks.dropna(subset=subset, how="all" if step.get("how") == "all" else "any").index
        return frame.loc[keep]
    if op == "remove_columns":
        return frame.drop(columns=[_column(frame, c, where) for c in _names(step["columns"], where)])
    if op == "keep_columns":
        return frame[[_column(frame, c, where) for c in _names(step["columns"], where)]]
    if op == "keep_rows":
        n = max(1, min(int(step["n"]), 10_000_000))
        return frame.tail(n) if step.get("mode") == "bottom" else frame.head(n)
    if op == "rename":
        old = _column(frame, step["column"], where)
        new = _name(step["name"], where)
        if new != old and new in frame.columns:
            raise StepError(f"{where}: a column called {new!r} already exists.")
        return frame.rename(columns={old: new})
    if op == "change_type":
        column = _column(frame, step["column"], where)
        frame = frame.copy()
        if step["type"] == "number":
            text = frame[column].astype("string").str.replace(r"[,\s$€£%]", "", regex=True)
            frame[column] = pd.to_numeric(text.replace({"": None}), errors="coerce")
        elif step["type"] == "date":
            frame[column] = pd.to_datetime(frame[column], errors="coerce", format="mixed")
        elif step["type"] == "text":
            frame[column] = frame[column].map(lambda v: None if pd.isna(v) else str(v))
        else:
            raise StepError(f"{where}: the type must be number, date or text.")
        return frame
    if op == "trim":
        frame = frame.copy()
        columns = [_column(frame, c, where) for c in step.get("columns") or []] or list(
            frame.select_dtypes(include="object").columns
        )
        for column in columns:
            frame[column] = frame[column].map(
                lambda v: re.sub(r"\s+", " ", v).strip() if isinstance(v, str) else v
            )
        return frame
    if op == "text_case":
        column = _column(frame, step["column"], where)
        case = step["case"]
        if case not in {"upper", "lower", "title"}:
            raise StepError(f"{where}: the case must be upper, lower or title.")
        frame = frame.copy()
        frame[column] = frame[column].map(lambda v: getattr(v, case)() if isinstance(v, str) else v)
        return frame
    if op == "replace_values":
        column = _column(frame, step["column"], where)
        find, replace = str(step["find"]), step.get("replace", "")
        frame = frame.copy()
        if step.get("match") == "contains":
            frame[column] = frame[column].map(
                lambda v: v.replace(find, str(replace)) if isinstance(v, str) else v
            )
        else:
            numeric = pd.api.types.is_numeric_dtype(frame[column])
            target: Any = find
            if numeric:
                if not _is_number(find):
                    # Text such as "N/A" was already read as blank in a column of numbers.
                    return frame
                if replace not in ("", None) and not _is_number(replace):
                    raise StepError(f"{where}: this column holds numbers; replace with a number.")
                target = float(find)
                replace = float(replace) if replace not in ("", None) else np.nan
            frame[column] = frame[column].mask(frame[column].astype(object) == target, replace)
        return frame
    if op == "fill_blanks":
        column = _column(frame, step["column"], where)
        strategy = step["strategy"]
        frame = frame.copy()
        series = frame[column].replace(r"^\s*$", np.nan, regex=True)
        if strategy in {"mean", "median"}:
            numbers = pd.to_numeric(series, errors="coerce")
            frame[column] = numbers.fillna(getattr(numbers, strategy)())
        elif strategy == "mode":
            mode = series.mode(dropna=True)
            frame[column] = series.fillna(mode.iloc[0]) if not mode.empty else series
        elif strategy == "zero":
            frame[column] = series.fillna(0)
        elif strategy == "down":
            frame[column] = series.ffill()
        elif strategy == "up":
            frame[column] = series.bfill()
        elif strategy == "value":
            value = step.get("value", "")
            frame[column] = series.fillna(
                float(value)
                if pd.api.types.is_numeric_dtype(series) and _is_number(value)
                else str(value)[:200]
            )
        else:
            raise StepError(f"{where}: unknown fill strategy {strategy!r}.")
        return frame
    if op == "filter_rows":
        mask = _mask(frame, step["conditions"], where, "or" if step.get("combine") == "or" else "and")
        return frame[~mask] if step.get("mode") == "remove" else frame[mask]
    if op == "sort":
        by = step["by"] if isinstance(step["by"], list) else []
        if not by:
            raise StepError(f"{where}: choose columns to sort by.")
        columns = [_column(frame, item.get("column"), where) for item in by[:10]]
        ascending = [item.get("dir") != "desc" for item in by[:10]]
        return frame.sort_values(columns, ascending=ascending, kind="stable", na_position="last")
    if op == "split_column":
        column = _column(frame, step["column"], where)
        parts = max(2, min(int(step.get("into") or 2), 10))
        pieces = (
            frame[column]
            .astype("string")
            .str.split(str(step["delimiter"])[:10], n=parts - 1, expand=True, regex=False)
        )
        names = list(step.get("names") or [])
        frame = frame.copy()
        for index in range(pieces.shape[1]):
            name = names[index] if index < len(names) and names[index] else f"{column}_{index + 1}"
            frame[_fresh(frame, name, where)] = pieces[index].str.strip()
        return frame
    if op == "merge_columns":
        columns = [_column(frame, c, where) for c in _names(step["columns"], where, 20)]
        name = _fresh(frame, step["name"], where)
        separator = str(step.get("separator", " "))[:10]
        frame = frame.copy()
        frame[name] = frame[columns].apply(
            lambda row: separator.join("" if pd.isna(v) else str(v) for v in row), axis=1
        )
        return frame
    if op == "date_parts":
        column = _column(frame, step["column"], where)
        parts = step["parts"] if isinstance(step["parts"], list) else []
        if not parts or not set(parts) <= DATE_PARTS:
            raise StepError(f"{where}: choose date parts ({', '.join(sorted(DATE_PARTS))}).")
        dates = pd.to_datetime(frame[column], errors="coerce", format="mixed")
        frame = frame.copy()
        for part in parts:
            name = f"{column}_{part}"
            if part == "week":
                frame[name] = dates.dt.isocalendar().week.astype("Int64")
            elif part == "month_name":
                frame[name] = dates.dt.month_name()
            elif part == "weekday_name":
                frame[name] = dates.dt.day_name()
            else:
                frame[name] = getattr(dates.dt, part)
        return frame
    if op == "conditional_column":
        name = _fresh(frame, step["name"], where)
        result = pd.Series([step.get("else")] * len(frame.index), index=frame.index, dtype=object)
        decided = pd.Series(False, index=frame.index)
        for rule in step["rules"]:
            hit = _mask(frame, [rule], where) & ~decided
            result[hit] = rule.get("then")
            decided |= hit
        frame = frame.copy()
        numeric = all(_is_number(r.get("then")) for r in step["rules"]) and _is_number(step.get("else", 0))
        frame[name] = pd.to_numeric(result, errors="coerce") if numeric else result
        return frame
    if op == "calculated_column":
        name = _fresh(frame, step["name"], where)
        try:
            tree = _expr.row_tree(step["expression"], list(frame.columns))
        except _expr.ExprError as exc:
            raise StepError(f"{where}: {exc}") from exc
        used = {node[1] for node in _expr._walk(tree) if node[0] == "col"}
        leaves = {("col", c): pd.to_numeric(frame[c], errors="coerce").to_numpy(dtype=float) for c in used}
        frame = frame.copy()
        frame[name] = _expr.evaluate(tree, leaves, len(frame.index))
        return frame
    if op == "bins":
        column = _column(frame, step["column"], where)
        numbers = pd.to_numeric(frame[column], errors="coerce")
        name = _fresh(frame, step.get("name") or f"{column}_bin", where)
        if step.get("method") == "edges":
            edges = sorted({float(e) for e in step.get("edges") or [] if _is_number(e)})
            if len(edges) < 2:
                raise StepError(f"{where}: give at least two bin edges.")
            binned = pd.cut(numbers, bins=edges, include_lowest=True)
        else:
            count = max(2, min(int(step.get("count") or 5), 50))
            binned = pd.cut(numbers, bins=count, include_lowest=True)
        frame = frame.copy()
        frame[name] = binned.map(
            lambda interval: (
                None if pd.isna(interval) else f"{_short(interval.left)}–{_short(interval.right)}"
            )
        ).astype(object)
        return frame
    if op == "group_by":
        by = [_column(frame, c, where) for c in _names(step["by"], where, 10)]
        aggregations = step["aggregations"] if isinstance(step["aggregations"], list) else []
        if not 1 <= len(aggregations) <= 12:
            raise StepError(f"{where}: add 1–12 aggregations.")
        grouped = frame.groupby(by, dropna=False, sort=True)
        out = grouped.size().rename("__rows").to_frame()
        for item in aggregations:
            agg = item.get("agg") or "sum"
            if agg not in AGGS:
                raise StepError(f"{where}: unknown aggregation {agg!r}.")
            source = item.get("column") or "*"
            label = _name(item.get("name") or (f"{agg}_{source}" if source != "*" else "rows"), where)
            if source == "*":
                out[label] = out["__rows"]
                continue
            _column(frame, source, where)
            series = grouped[source]
            if agg in {"sum", "avg", "median"}:
                numeric = pd.to_numeric(frame[source], errors="coerce").groupby(
                    [frame[c] for c in by], dropna=False
                )
                out[label] = getattr(numeric, {"avg": "mean"}.get(agg, agg))()
            elif agg == "distinct":
                out[label] = series.nunique()
            elif agg == "first":
                out[label] = series.first()
            elif agg == "last":
                out[label] = series.last()
            else:
                out[label] = getattr(series, agg)()
        return out.drop(columns="__rows").reset_index()
    if op == "pivot":
        index = [_column(frame, c, where) for c in _names(step["index"], where, 10)]
        columns = _column(frame, step["columns"], where)
        values = _column(frame, step["values"], where)
        if frame[columns].nunique() > 200:
            raise StepError(f"{where}: {columns!r} has more than 200 values to turn into columns.")
        agg = step.get("agg") or "sum"
        if agg not in AGGS - {"distinct", "first", "last"}:
            raise StepError(f"{where}: unknown aggregation {agg!r}.")
        source = pd.to_numeric(frame[values], errors="coerce") if agg != "count" else frame[values]
        table = (
            frame.assign(**{values: source})
            .pivot_table(index=index, columns=columns, values=values, aggfunc={"avg": "mean"}.get(agg, agg))
            .reset_index()
        )
        table.columns = [str(c) for c in table.columns]
        return table
    if op == "unpivot":
        keep = [_column(frame, c, where) for c in _names(step["keep"], where)]
        attribute = _name(step.get("name") or "attribute", where)
        value = _name(step.get("value") or "value", where)
        values = [c for c in frame.columns if c not in keep]
        if not values:
            raise StepError(f"{where}: there are no other columns to unpivot.")
        return frame.melt(id_vars=keep, value_vars=values, var_name=attribute, value_name=value)
    if op == "merge":
        other = others.get(str(step["dataset"]))
        if other is None:
            raise StepError(f"{where}: the other dataset is not available.")
        left = _column(frame, step["left_on"], where)
        if step["right_on"] not in other.columns:
            raise StepError(f"{where}: the other dataset has no column {step['right_on']!r}.")
        how = step.get("how") if step.get("how") in {"left", "inner", "outer"} else "left"
        merged = frame.merge(
            other,
            how=how,
            left_on=left,
            right_on=step["right_on"],
            suffixes=("", str(step.get("suffix") or "_2")[:20]),
        )
        if len(merged.index) > int(limits.get("max_rows", 1_000_000)):
            raise StepError(f"{where}: the merge produces too many rows; check the key columns.")
        return merged
    if op == "append":
        other = others.get(str(step["dataset"]))
        if other is None:
            raise StepError(f"{where}: the other dataset is not available.")
        return pd.concat([frame, other], ignore_index=True, sort=False)
    if op == "outliers":
        column = _column(frame, step["column"], where)
        numbers = pd.to_numeric(frame[column], errors="coerce")
        q1, q3 = numbers.quantile(0.25), numbers.quantile(0.75)
        spread = float(step.get("k") or 1.5) * (q3 - q1)
        low, high = q1 - spread, q3 + spread
        outside = (numbers < low) | (numbers > high)
        frame = frame.copy()
        action = step["action"]
        if action == "cap":
            frame[column] = numbers.clip(lower=low, upper=high)
        elif action == "remove":
            frame = frame[~outside.fillna(False)]
        elif action == "flag":
            frame[_fresh(frame, f"{column}_outlier", where)] = outside.fillna(False)
        else:
            raise StepError(f"{where}: the action must be cap, remove or flag.")
        return frame
    if op == "date_table":
        column = _column(frame, step["column"], where)
        dates = pd.to_datetime(frame[column], errors="coerce", format="mixed").dropna()
        if dates.empty:
            raise StepError(f"{where}: {column!r} has no dates.")
        days = pd.date_range(dates.min().normalize(), dates.max().normalize(), freq="D")
        if len(days) > 50_000:
            raise StepError(f"{where}: the date range is longer than 50,000 days.")
        start = max(1, min(int(step.get("fiscal_start") or 1), 12))
        fiscal = days.year + (days.month >= start).astype(int) if start > 1 else days.year
        return pd.DataFrame(
            {
                "date": days,
                "year": days.year,
                "quarter": days.quarter,
                "month": days.month,
                "month_name": days.month_name(),
                "week": days.isocalendar().week.astype(int).to_numpy(),
                "weekday": days.day_name(),
                "day": days.day,
                "is_weekend": days.weekday >= 5,
                "fiscal_year": fiscal,
            }
        )
    raise StepError(f"{where}: unknown step.")


def _is_number(value: Any) -> bool:
    try:
        return value is not None and value != "" and not math.isnan(float(value))
    except TypeError, ValueError:
        return False


def _short(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.2f}".rstrip("0").rstrip(".")


def run(
    frame: Any,
    pipeline: list[dict[str, Any]],
    others: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    upto: int | None = None,
) -> Any:
    """Apply the (validated) pipeline; ``upto`` stops after that step (0-based, inclusive)."""
    steps = pipeline if upto is None else pipeline[: max(0, int(upto)) + 1]
    for index, step in enumerate(steps):
        if not step.get("enabled", True):
            continue
        try:
            frame = _apply(step, frame, others or {}, limits or {})
        except StepError as exc:
            raise StepError(f"Step {index + 1} — {exc}") from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise StepError(f"Step {index + 1} ({step['label']}): {str(exc)[:200]}") from exc
        frame = frame.reset_index(drop=True)
        if len(frame.columns) > 2_000:
            raise StepError(f"Step {index + 1} ({step['label']}): too many columns.")
    return frame
