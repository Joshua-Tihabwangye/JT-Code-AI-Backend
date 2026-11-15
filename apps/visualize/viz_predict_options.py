"""What a dataset can predict: read by pandas, it fills the "Create your own prediction" form.

For every column: its kind, how many distinct values and blanks it has, whether it is an identifier
or constant, which kinds of prediction it can be the target of (with the reason when it cannot), and
whether it is a sensible input. For every date column: first and last date, number of periods and the
natural time grain. The form shows exactly these choices, so the user only picks.
"""

from __future__ import annotations

import re
from typing import Any

GOALS = ("forecast", "estimate", "classify", "drivers", "anomaly", "segments")
ID_NAME = re.compile(
    r"(^id$|_id$|^id_|\bid\b|uuid|guid|sku|_code$|^code$|_key$|_no$|_number$|^number$)", re.I
)
MAX_CLASSES = 50
MIN_ROWS = 20
MIN_PERIODS = 8
MAX_MISSING = 0.6


def _grain(dates: Any) -> str:
    """The natural spacing of the dates: day, week, month, quarter or year."""
    import numpy as np

    unique = np.sort(dates.dropna().unique())
    if len(unique) < 2:
        return "day"
    gap_days = float(np.median(np.diff(unique)) / np.timedelta64(1, "D"))
    if gap_days <= 1.5:
        return "day"
    if gap_days <= 8:
        return "week"
    if gap_days <= 35:
        return "month"
    if gap_days <= 100:
        return "quarter"
    return "year"


def options(frame: Any, kinds: dict[str, str]) -> dict[str, Any]:
    import pandas as pd

    rows = len(frame.index)
    date_columns = []
    for name, kind in kinds.items():
        if kind != "datetime":
            continue
        values = pd.to_datetime(frame[name], errors="coerce", utc=True)
        present = values.dropna()
        if present.empty:
            continue
        date_columns.append(
            {
                "name": str(name),
                "first": present.min().date().isoformat(),
                "last": present.max().date().isoformat(),
                "periods": int(present.nunique()),
                "grain": _grain(values),
            }
        )
    can_forecast = any(entry["periods"] >= MIN_PERIODS for entry in date_columns)

    columns = []
    for name in frame.columns:
        kind = kinds.get(name, "categorical")
        series = frame[name]
        present = int(series.notna().sum())
        distinct = int(series.nunique(dropna=True))
        missing = round(1 - present / rows, 4) if rows else 1.0
        identifier = bool(ID_NAME.search(str(name))) or (
            kind != "numeric" and rows >= MIN_ROWS and distinct >= 0.95 * max(present, 1)
        )
        constant = distinct <= 1
        free_text = kind in {"text", "categorical"} and distinct > MAX_CLASSES
        unusable = (
            "constant"
            if constant
            else "identifier"
            if identifier
            else "mostly_empty"
            if missing > MAX_MISSING
            else "few_rows"
            if rows < MIN_ROWS
            else ""
        )
        goals: dict[str, str] = {}  # goal -> "" when possible, else the reason it is not
        for goal in GOALS:
            reason = unusable
            if not reason:
                if kind == "datetime":
                    reason = "is_date"
                elif goal == "forecast":
                    reason = (
                        ""
                        if kind == "numeric" and can_forecast
                        else ("needs_number" if kind != "numeric" else "needs_dates")
                    )
                elif goal == "estimate":
                    reason = "" if kind == "numeric" else "needs_number"
                elif goal == "classify":
                    reason = (
                        ""
                        if (kind != "numeric" or distinct <= 10) and 2 <= distinct <= MAX_CLASSES
                        else "too_many_values"
                    )
                elif goal == "anomaly":
                    reason = "" if kind == "numeric" else "needs_number"
                elif goal == "drivers":
                    reason = "" if not free_text else "too_many_values"
                else:  # segments
                    reason = ""
            goals[goal] = reason
        top = series.dropna().astype(str).value_counts().head(MAX_CLASSES) if not identifier else None
        columns.append(
            {
                "name": str(name),
                "kind": kind,
                "distinct": distinct,
                "missing": missing,
                "identifier": identifier,
                "constant": constant,
                "goals": goals,
                # A sensible input for models: not an ID, constant, mostly empty, free text or a date.
                "input": not (
                    identifier or constant or free_text or missing > MAX_MISSING or kind == "datetime"
                ),
                "values": [str(v) for v in top.index] if top is not None and distinct <= MAX_CLASSES else [],
            }
        )
    return {"rows": rows, "columns": columns, "dateColumns": date_columns}
