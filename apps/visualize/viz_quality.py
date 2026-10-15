"""Data-quality scan: finds problems in an uploaded dataset without changing it.

Runs inside the isolated engine on the raw text of the CSV (every cell exactly as
uploaded), so blanks, stray spaces and placeholders such as "N/A" are visible.
Each check records the rows and column it affects; the API lists the issues and
the grid colours the affected cells. Nothing here edits data: fixing is the
user's decision, in the tool or in the source file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

ISSUE_TYPES: dict[str, dict[str, str]] = {
    "duplicate_row": {"label": "Duplicate row", "color": "#8B5CF6", "severity": "warning"},
    "duplicate_id": {"label": "Duplicate ID", "color": "#DB2777", "severity": "error"},
    "missing_id": {"label": "Missing ID", "color": "#E11D48", "severity": "error"},
    "id_gap": {"label": "Missing IDs in a sequence", "color": "#BE123C", "severity": "warning"},
    "missing_value": {"label": "Missing value", "color": "#F59E0B", "severity": "warning"},
    "placeholder": {"label": "Placeholder value", "color": "#FB923C", "severity": "warning"},
    "type_mismatch": {"label": "Wrong type", "color": "#C026D3", "severity": "error"},
    "invalid_date": {"label": "Invalid or implausible date", "color": "#0D9488", "severity": "warning"},
    "invalid_email": {"label": "Invalid email", "color": "#9333EA", "severity": "error"},
    "outlier": {"label": "Extreme outlier", "color": "#EF4444", "severity": "info"},
    "negative": {"label": "Unexpected negative", "color": "#EA580C", "severity": "warning"},
    "inconsistent_category": {"label": "Inconsistent spelling", "color": "#0EA5E9", "severity": "warning"},
    "whitespace": {"label": "Extra spaces", "color": "#64748B", "severity": "info"},
    "constant_column": {"label": "Constant column", "color": "#94A3B8", "severity": "info"},
}
PLACEHOLDERS = {
    "n/a",
    "na",
    "null",
    "none",
    "nil",
    "-",
    "--",
    "?",
    "undefined",
    "#n/a",
    "nan",
    "missing",
    "#value!",
    "#ref!",
    "#div/0!",
}
ID_NAME = re.compile(
    r"(^id$|_id$|^id_|\bid\b|uuid|guid|sku|_code$|^code$|_key$|_no$|_number$|^number$)", re.I
)
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
NONNEGATIVE_NAME = re.compile(
    r"(amount|price|qty|quantity|units|count|age|cost|revenue|sales|total|salary|weight|height|stock|distance|area|charges|income|volume|sessions)",
    re.I,
)
MAX_ROWS_PER_ISSUE = 500


@dataclass
class Finding:
    type: str
    column: str | None
    rows: Any  # numpy array of 0-based row positions
    detail: str
    suggestion: str
    examples: list[str] = field(default_factory=list)
    count: int | None = None


def raw_frame(content: bytes, limits: dict[str, Any]) -> Any:
    """The CSV with every cell as the exact text that was uploaded."""
    import io

    import pandas as pd

    text = content.decode("utf-8-sig")
    frame = pd.read_csv(
        io.StringIO(text), dtype=str, keep_default_na=False, na_filter=False, nrows=limits["max_rows"] + 1
    )
    return frame


def _numbers(stripped: Any) -> Any:
    import pandas as pd

    return pd.to_numeric(stripped.str.replace(r"[,\s$€£%]", "", regex=True), errors="coerce")


def scan(raw: Any) -> list[Finding]:
    import numpy as np
    import pandas as pd

    findings: list[Finding] = []
    rows = len(raw.index)
    if not rows:
        return findings
    stripped_frame = raw.apply(lambda s: s.str.strip())

    duplicated = stripped_frame.duplicated(keep="first").to_numpy()
    if duplicated.any():
        findings.append(
            Finding(
                "duplicate_row",
                None,
                np.flatnonzero(duplicated),
                f"{int(duplicated.sum()):,} row(s) repeat an earlier row exactly.",
                "Delete the repeated rows unless they are genuine repeat records.",
                count=int(duplicated.sum()),
            )
        )

    for column in raw.columns:
        raw_values = raw[column]
        stripped = stripped_frame[column]
        lowered = stripped.str.lower()
        blank = (stripped == "").to_numpy()
        placeholder = lowered.isin(PLACEHOLDERS).to_numpy()
        missing = blank | placeholder
        present = ~missing
        present_values = stripped[present]
        numbers = _numbers(stripped)
        numeric_share = float(numbers[present].notna().mean()) if present.any() else 0.0
        is_numeric = present.sum() >= 5 and numeric_share >= 0.9
        # Named ID columns, or unique text codes; unique numeric measures are not IDs.
        is_id = bool(ID_NAME.search(str(column))) or (
            not is_numeric
            and rows >= 20
            and present_values.nunique() >= 0.98 * max(present.sum(), 1)
            and present_values.str.len().mean() <= 40
            and present_values.str.contains(" ").mean() <= 0.3
        )
        is_date = False
        parsed_dates = None
        if (
            not is_numeric
            and present.sum() >= 5
            and present_values.str.contains(r"\d", regex=True).mean() > 0.9
        ):
            parsed_dates = pd.to_datetime(stripped.where(present), errors="coerce", format="mixed", utc=True)
            is_date = float(parsed_dates[present].notna().mean()) >= 0.9
        if is_id and is_date:
            is_id = False

        if is_id:
            if missing.any():
                findings.append(
                    Finding(
                        "missing_id",
                        column,
                        np.flatnonzero(missing),
                        f"{int(missing.sum()):,} row(s) have no {column}.",
                        f"Fill in the missing {column} values so every row can be identified.",
                    )
                )
            dup = (stripped.duplicated(keep=False) & pd.Series(present, index=stripped.index)).to_numpy()
            if dup.any():
                examples = list(stripped[dup].value_counts().head(5).index)
                findings.append(
                    Finding(
                        "duplicate_id",
                        column,
                        np.flatnonzero(dup),
                        f"{len(stripped[dup].unique()):,} {column} value(s) are used by more than one row.",
                        "IDs should be unique: correct or merge the rows sharing an ID.",
                        examples=[str(v) for v in examples],
                    )
                )
            findings.extend(_id_gaps(column, present_values))
        else:
            if blank.any():
                findings.append(
                    Finding(
                        "missing_value",
                        column,
                        np.flatnonzero(blank),
                        f"{int(blank.sum()):,} blank cell(s) in {column} ({blank.mean():.1%}).",
                        "Fill the blanks or decide whether those rows should be excluded.",
                    )
                )
            if placeholder.any():
                examples = list(stripped[placeholder].value_counts().head(5).index)
                findings.append(
                    Finding(
                        "placeholder",
                        column,
                        np.flatnonzero(placeholder),
                        (
                            f"{int(placeholder.sum()):,} cell(s) in {column} hold a placeholder instead "
                            "of a value."
                        ),
                        "Replace placeholders such as “N/A” with a real value or leave the cell empty.",
                        examples=[str(v) for v in examples],
                    )
                )

        spaces = ((raw_values != stripped) & ~pd.Series(blank, index=raw.index)).to_numpy()
        if spaces.any():
            findings.append(
                Finding(
                    "whitespace",
                    column,
                    np.flatnonzero(spaces),
                    f"{int(spaces.sum()):,} value(s) in {column} have leading or trailing spaces.",
                    "Trim the spaces; they make identical values look different.",
                )
            )

        if is_numeric and not is_id:
            bad = present & numbers.isna().to_numpy()
            if bad.any():
                findings.append(
                    Finding(
                        "type_mismatch",
                        column,
                        np.flatnonzero(bad),
                        f"{int(bad.sum()):,} value(s) in the numeric column {column} are not numbers.",
                        "Correct the text entries or move them to a notes column.",
                        examples=[str(v) for v in stripped[bad].value_counts().head(5).index],
                    )
                )
            values = numbers[present & numbers.notna().to_numpy()]
            if len(values) >= 10:
                median = float(values.median())
                mad = float((values - median).abs().median()) * 1.4826
                if mad > 0:
                    extreme = ((numbers - median).abs() / mad > 5).fillna(False).to_numpy()
                else:
                    q1, q3 = values.quantile([0.25, 0.75])
                    spread = q3 - q1
                    extreme = (
                        ((numbers < q1 - 3 * spread) | (numbers > q3 + 3 * spread)).fillna(False).to_numpy()
                        if spread > 0
                        else np.zeros(rows, bool)
                    )
                if extreme.any() and extreme.sum() <= max(3, 0.05 * len(values)):
                    findings.append(
                        Finding(
                            "outlier",
                            column,
                            np.flatnonzero(extreme),
                            (
                                f"{int(extreme.sum()):,} value(s) in {column} are far from the rest "
                                f"(median {median:,.4g})."
                            ),
                            (
                                "Check whether these are data-entry errors (an extra zero, wrong unit) "
                                "or genuine extremes."
                            ),
                            examples=[str(v) for v in stripped[extreme][:5]],
                        )
                    )
                negative = (numbers < 0).fillna(False).to_numpy()
                share = float((values >= 0).mean())
                if negative.any() and (
                    share >= 0.99 or (share >= 0.9 and NONNEGATIVE_NAME.search(str(column)))
                ):
                    findings.append(
                        Finding(
                            "negative",
                            column,
                            np.flatnonzero(negative),
                            (
                                f"{int(negative.sum()):,} negative value(s) in {column}, which is "
                                "otherwise positive."
                            ),
                            "Confirm whether negatives are valid here (refunds, corrections) or sign errors.",
                        )
                    )
        elif is_date and parsed_dates is not None:
            unparsable = present & parsed_dates.isna().to_numpy()
            now = pd.Timestamp.now(tz="UTC")
            implausible = (
                parsed_dates.notna()
                & (
                    (parsed_dates > now + pd.Timedelta(days=366))
                    | (parsed_dates < pd.Timestamp("1900-01-01", tz="UTC"))
                )
            ).to_numpy()
            bad = unparsable | implausible
            if bad.any():
                findings.append(
                    Finding(
                        "invalid_date",
                        column,
                        np.flatnonzero(bad),
                        (
                            f"{int(unparsable.sum()):,} unreadable and {int(implausible.sum()):,} "
                            f"implausible date(s) in {column}."
                        ),
                        (
                            "Use one date format (e.g. 2025-01-31) and check dates far in the future "
                            "or before 1900."
                        ),
                        examples=[str(v) for v in stripped[bad][:5]],
                    )
                )
        elif not is_id and present.any():
            if "email" in str(column).lower() or present_values.str.match(EMAIL.pattern).mean() >= 0.8:
                invalid = present & ~stripped.str.match(EMAIL.pattern).to_numpy()
                if invalid.any():
                    findings.append(
                        Finding(
                            "invalid_email",
                            column,
                            np.flatnonzero(invalid),
                            f"{int(invalid.sum()):,} value(s) in {column} are not valid email addresses.",
                            "Correct the addresses (missing @, domain or typos).",
                            examples=[str(v) for v in stripped[invalid][:5]],
                        )
                    )
            elif present_values.nunique() <= 500:
                findings.extend(_inconsistent(column, stripped, present))
        if present.any() and present_values.nunique() == 1 and rows > 1:
            findings.append(
                Finding(
                    "constant_column",
                    column,
                    np.array([], dtype=int),
                    f"Every row of {column} has the same value ({present_values.iloc[0]!s:.40}).",
                    "A constant column carries no information; consider removing it.",
                    count=0,
                )
            )
    return findings


def _id_gaps(column: str, values: Any) -> list[Finding]:
    import numpy as np

    match = values.str.extract(r"^(?P<prefix>\D*?)(?P<number>\d{1,12})$")
    if match["number"].isna().mean() > 0.05 or len(values) < 5:
        return []
    prefixes = match["prefix"].fillna("")
    if prefixes.nunique() > 1:
        return []
    numbers = np.unique(match["number"].dropna().astype("int64").to_numpy())
    span = int(numbers.max() - numbers.min() + 1)
    missing_count = span - len(numbers)
    if missing_count <= 0 or span > 1.5 * len(numbers) or span > 5_000_000:
        return []
    present = set(numbers.tolist())
    gaps = [n for n in range(int(numbers.min()), int(numbers.max()) + 1) if n not in present][:20]
    digits = match["number"].dropna()
    padded = bool((digits.str.len() > 1).any() and digits.str.startswith("0").any())
    width = int(digits.str.len().max()) if padded else 0
    prefix = prefixes.iloc[0]
    examples = [f"{prefix}{str(n).zfill(width)}" for n in gaps[:10]]
    return [
        Finding(
            "id_gap",
            column,
            np.array([], dtype=int),
            (
                f"{missing_count:,} ID(s) are missing from the sequence "
                f"{prefix}{numbers.min()}…{prefix}{numbers.max()}."
            ),
            "Check whether those records were deleted or never imported.",
            examples=examples,
            count=missing_count,
        )
    ]


def _inconsistent(column: str, stripped: Any, present: Any) -> list[Finding]:
    import numpy as np

    keys = stripped.str.lower().str.replace(r"[\s._-]+", " ", regex=True).str.strip()
    frame = stripped[present].to_frame("value").assign(key=keys[present])
    flagged = np.zeros(len(stripped), dtype=bool)
    examples: list[str] = []
    for _key, group in frame.groupby("key"):
        counts = group["value"].value_counts()
        if len(counts.index) > 1:
            minority = set(counts.index[1:])
            flagged |= stripped.isin(minority).to_numpy() & present
            examples.append(" / ".join(str(v) for v in counts.index[:3]))
    if not flagged.any():
        return []
    return [
        Finding(
            "inconsistent_category",
            column,
            np.flatnonzero(flagged),
            f"{len(examples)} value(s) in {column} are spelled in more than one way.",
            "Use one spelling for each category so they group together.",
            examples=examples[:5],
        )
    ]


def summarize(raw: Any, findings: list[Finding]) -> dict[str, Any]:
    import numpy as np

    rows, columns = len(raw.index), len(raw.columns)
    affected = np.zeros(rows, dtype=bool)
    cells = 0
    issues = []
    for index, finding in enumerate(findings):
        meta = ISSUE_TYPES[finding.type]
        if len(finding.rows):
            affected[finding.rows] = True
            cells += len(finding.rows) * (columns if finding.column is None else 1)
        count = finding.count if finding.count is not None else int(len(finding.rows))
        issues.append(
            {
                "id": f"{finding.type}-{index}",
                "type": finding.type,
                "label": meta["label"],
                "color": meta["color"],
                "severity": meta["severity"],
                "column": finding.column,
                "count": count,
                "rows": [int(r) for r in finding.rows[:MAX_ROWS_PER_ISSUE]],
                "detail": finding.detail,
                "suggestion": finding.suggestion,
                "examples": finding.examples,
            }
        )
    severity_rank = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda issue: (severity_rank[issue["severity"]], -issue["count"]))
    totals: dict[str, int] = {}
    for issue in issues:
        totals[issue["type"]] = totals.get(issue["type"], 0) + issue["count"]
    score = max(0.0, 100.0 * (1 - cells / max(rows * columns, 1)))
    return {
        "summary": {
            "rows": rows,
            "columns": columns,
            "issues": len(issues),
            "affectedRows": int(affected.sum()),
            "flaggedCells": int(cells),
            "score": round(score, 1),
        },
        "types": [
            {"type": key, **ISSUE_TYPES[key], "count": totals[key]} for key in ISSUE_TYPES if key in totals
        ],
        "issues": issues,
    }


def row_flags(findings: list[Finding], positions: list[int]) -> dict[int, dict[str, list[str]]]:
    """Issue types per cell for the given row positions (``"*"`` = the whole row)."""
    import numpy as np

    wanted = np.asarray(positions, dtype=int)
    flags: dict[int, dict[str, list[str]]] = {}
    for finding in findings:
        if not len(finding.rows):
            continue
        hits = np.intersect1d(finding.rows, wanted, assume_unique=False)
        for row in hits.tolist():
            flags.setdefault(int(row), {}).setdefault(finding.column or "*", []).append(finding.type)
    return flags


def flagged_rows(findings: list[Finding], issue_type: str | None = None) -> Any:
    import numpy as np

    chosen = [
        f.rows for f in findings if len(f.rows) and (issue_type in (None, "issues") or f.type == issue_type)
    ]
    return np.unique(np.concatenate(chosen)) if chosen else np.array([], dtype=int)
