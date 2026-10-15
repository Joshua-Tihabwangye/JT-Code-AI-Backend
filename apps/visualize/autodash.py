"""A starter dashboard from a dataset's profile (rule-based, free — no AI).

Layout on the 24-column grid of a 1280×720 page (rows of 24 px), so it fits a 1366×768 screen:

* row 0: slicers — date range plus up to three low-cardinality categories;
* row 3: up to six KPI cards, five rows tall (sums for amounts, averages for rates and scores, a target when a
  matching ``target_*``/``budget`` column exists, a trend sparkline when there is a date);
* row 8: the main trend (2/3) and a composition donut (1/3);
* row 18: a Top-10 ranking, a category × category matrix and a map when a column holds countries.
"""

from __future__ import annotations

import re
from typing import Any

from apps.visualize import recommend

AVERAGE_HINTS = (
    "rate",
    "ratio",
    "score",
    "satisfaction",
    "percent",
    "pct",
    "price",
    "discount",
    "age",
    "rating",
)
TARGET_HINTS = ("target", "goal", "budget", "quota", "plan")
GEO_HINTS = ("country", "nation", "iso3", "iso")


def _is_target(name: str) -> bool:
    return any(hint in name.lower() for hint in TARGET_HINTS)


def _agg(name: str) -> str:
    return "avg" if any(hint in name.lower() for hint in AVERAGE_HINTS) else "sum"


def _target_for(measure: str, numeric: list[str]) -> str | None:
    base = measure.lower()
    for name in numeric:
        stripped = re.sub(r"(^|_)(target|goal|budget|quota|plan)(_|$)", "_", name.lower()).strip("_")
        if name != measure and _is_target(name) and stripped == base:
            return name
    return None


def build(dataset_id: str, profile: dict[str, Any], name: str = "Overview") -> dict[str, Any]:
    roles = recommend.column_roles(profile)
    numeric = roles["numeric"]
    measures = [n for n in numeric if not _is_target(n)]
    dates = roles["datetime"]
    cardinality = {e["name"]: int(e.get("distinct", 0)) for e in profile.get("columnProfiles", [])}
    dims = [d for d in roles["dimension"] if cardinality.get(d, 0) > 1]
    geo = next((d for d in dims if any(h in d.lower() for h in GEO_HINTS)), None)
    small = [d for d in dims if cardinality.get(d, 0) <= 6 and d != geo]
    medium = [d for d in dims if 2 < cardinality.get(d, 0) <= 40 and d != geo]
    date = dates[0] if dates else None
    visuals: list[dict[str, Any]] = []

    def add(
        vid: str, vtype: str, layout: tuple[int, int, int, int], wells: dict[str, Any], **extra: Any
    ) -> None:
        x, y, w, h = layout
        visuals.append(
            {
                "id": vid,
                "type": vtype,
                "layout": {"x": x, "y": y, "w": w, "h": h, "z": 0},
                "data": {"datasetId": dataset_id, "wells": wells, "filters": []},
                **extra,
            }
        )

    # Row 0 — slicers.
    slicers = ([date] if date else []) + [d for d in dims if cardinality.get(d, 0) <= 12 and d != geo][
        : 4 - (1 if date else 0)
    ]
    for index, field in enumerate(slicers):
        style = "date_range" if field == date else "dropdown"
        add(
            f"slicer-{index}",
            "slicer",
            (index * 6, 0, 6, 3),
            {"category": [{"field": field}]},
            format={"slicer": {"style": style}},
        )
    # Row 3 — KPI cards.
    kpis = measures[:6]
    width = 24 // max(len(kpis), 1) if kpis else 4
    for index, field in enumerate(kpis):
        wells: dict[str, Any] = {"values": [{"field": field, "agg": _agg(field)}]}
        target = _target_for(field, numeric)
        if target:
            wells["target"] = [{"field": target, "agg": _agg(field)}]
        if date:
            wells["category"] = [{"field": date}]
        add(f"kpi-{index}", "kpi", (index * width, 3, width, 5), wells, format={"kpi": {"grain": "month"}})
    if not kpis:
        add("kpi-0", "card", (0, 3, 6, 5), {"values": [{"field": "*"}]})
    main = measures[0] if measures else None
    second = measures[1] if len(measures) > 1 else None
    # Row 7 — trend and composition.
    if date and main:
        wells = {
            "category": [{"field": date, "grain": "month"}],
            "values": [{"field": main, "agg": _agg(main)}],
        }
        if second:
            wells["secondaryValues"] = [{"field": second, "agg": _agg(second)}]
        add("trend", "combo" if second else "line", (0, 8, 16, 10), wells)
    elif medium and main:
        add(
            "trend",
            "column",
            (0, 8, 16, 10),
            {"category": [{"field": medium[0]}], "values": [{"field": main}]},
        )
    if small:
        add(
            "share",
            "donut",
            (16, 8, 8, 10),
            {
                "category": [{"field": small[0]}],
                "values": [{"field": main, "agg": _agg(main)}] if main else [],
            },
        )
    # Row 18 — ranking, matrix, map.
    rank_by = second or main
    ranking = next((d for d in medium if d not in small[:1]), medium[0] if medium else None)
    if ranking:
        add(
            "ranking",
            "bar",
            (0, 18, 8, 12),
            {
                "category": [{"field": ranking}],
                "values": [{"field": rank_by, "agg": _agg(rank_by)}] if rank_by else [],
            },
            format={"topN": {"n": 10, "others": False}},
        )
    rows = ranking or (dims[0] if dims else None)
    columns = next((d for d in dims if d not in {rows, geo} and cardinality.get(d, 0) <= 8), None)
    if rows and columns:
        add(
            "matrix",
            "matrix",
            (8, 18, 8, 12),
            {
                "rows": [{"field": rows}],
                "columns": [{"field": columns}],
                "values": [{"field": main}] if main else [],
            },
        )
    if geo:
        add(
            "map",
            "map",
            (16, 18, 8, 12),
            {"category": [{"field": geo}], "values": [{"field": main}] if main else []},
        )
    elif medium[1:]:
        add(
            "ranking-2",
            "column",
            (16, 18, 8, 12),
            {"category": [{"field": medium[1]}], "values": [{"field": main}] if main else []},
        )
    return {
        "theme": "executive",
        "mode": "system",
        "pages": [{"id": "page-1", "name": name, "visuals": visuals}],
    }
