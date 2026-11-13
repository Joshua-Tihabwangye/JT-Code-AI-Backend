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


# Levels and rates are averaged (summing prices or scores is meaningless); flows and counts are added.
LEVEL_WORDS = {
    *AVERAGE_HINTS,
    "open",
    "high",
    "low",
    "close",
    "adj",
    "balance",
    "level",
    "index",
    "temperature",
    "temp",
    "speed",
    "weight",
    "height",
    "margin",
    "avg",
    "average",
    "mean",
    "median",
    "yield",
    "interest",
    "nps",
    "duration",
    "tenure",
    "latitude",
    "longitude",
    "lat",
    "lon",
    "bmi",
    "share",
    "growth",
    "stock",
}
FLOW_WORDS = {
    "sales",
    "revenue",
    "amount",
    "qty",
    "quantity",
    "units",
    "orders",
    "count",
    "cost",
    "costs",
    "profit",
    "volume",
    "clicks",
    "visits",
    "sessions",
    "spend",
    "income",
    "expense",
    "expenses",
    "payments",
    "transactions",
    "calls",
    "hours",
    "sold",
    "views",
    "downloads",
    "signups",
    "leads",
    "conversions",
    "impressions",
    "tickets",
    "deliveries",
    "returns",
    "total",
}


def _words(name: str) -> set[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).lower()
    return {word for word in re.split(r"[^a-z0-9]+", spaced) if word}


def _agg(name: str, stats: dict[str, dict[str, Any]] | None = None) -> str:
    words = _words(name)
    if words & FLOW_WORDS:
        return "sum"
    if words & LEVEL_WORDS or any(hint in name.lower() for hint in AVERAGE_HINTS):
        return "avg"
    column = (stats or {}).get(name, {})
    low, high = column.get("min"), column.get("max")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)) and low >= 0 and high <= 1:
        return "avg"  # a ratio between 0 and 1
    return "sum"


def _grain(profile: dict[str, Any], date: str | None) -> str:
    """The time grain that gives a readable number of points for the data's date span."""
    import datetime as dt

    entry = next((e for e in profile.get("columnProfiles", []) if e.get("name") == date), {})
    try:
        start = dt.datetime.fromisoformat(str(entry.get("min"))[:19])
        end = dt.datetime.fromisoformat(str(entry.get("max"))[:19])
    except TypeError, ValueError:
        return "month"
    days = (end - start).days
    if days <= 62:
        return "day"
    if days <= 270:
        return "week"
    if days <= 4 * 366:
        return "month"
    if days <= 12 * 366:
        return "quarter"
    return "year"


THEME_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("corporate", ("price", "close", "open", "ticker", "stock", "symbol", "balance", "interest", "yield")),
    ("crm", ("customer", "churn", "tenure", "plan", "contract", "subscriber", "lead", "campaign")),
    ("executive", ("revenue", "sales", "profit", "margin", "order", "region", "product")),
)


def _theme_for(columns: list[str]) -> str:
    """A colour theme that suits the subject: finance, customers, sales; JT Vivid otherwise."""
    words = set().union(*(_words(column) for column in columns)) if columns else set()
    for theme, hints in THEME_HINTS:
        if sum(1 for hint in hints if hint in words) >= 2:
            return theme
    return "vivid"


def _target_for(measure: str, numeric: list[str]) -> str | None:
    base = measure.lower()
    for name in numeric:
        stripped = re.sub(r"(^|_)(target|goal|budget|quota|plan)(_|$)", "_", name.lower()).strip("_")
        if name != measure and _is_target(name) and stripped == base:
            return name
    return None


def build(dataset_id: str, profile: dict[str, Any], name: str = "Overview") -> dict[str, Any]:
    stats = {e["name"]: e for e in profile.get("columnProfiles", []) if "name" in e}

    def agg(column: str) -> str:
        return _agg(column, stats)

    roles = recommend.column_roles(profile)
    numeric = roles["numeric"]
    measures = [n for n in numeric if not _is_target(n)]
    # Price data leads with the closing price (the standard headline), not open/high/low.
    headline = next((m for m in measures if _words(m) & {"close", "price"}), None)
    if headline and measures and _words(measures[0]) & {"open", "high", "low"}:
        measures.remove(headline)
        measures.insert(0, headline)
    dates = roles["datetime"]
    cardinality = {e["name"]: int(e.get("distinct", 0)) for e in profile.get("columnProfiles", [])}
    dims = [d for d in roles["dimension"] if cardinality.get(d, 0) > 1]
    geo = next((d for d in dims if any(h in d.lower() for h in GEO_HINTS)), None)
    small = [d for d in dims if cardinality.get(d, 0) <= 6 and d != geo]
    medium = [d for d in dims if 2 < cardinality.get(d, 0) <= 40 and d != geo]
    date = dates[0] if dates else None
    grain = _grain(profile, date)
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

    main = measures[0] if measures else None
    second = measures[1] if len(measures) > 1 else None
    # A metric switcher (field parameter) over the main measures, bound to the ranking below.
    switch = measures[:3] if len(measures) >= 2 else []
    parameters = (
        [
            {
                "id": "metric",
                "name": "Metric",
                "kind": "fields",
                "role": "measure",
                "options": [{"field": m, "agg": agg(m)} for m in switch],
                "default": 1 if len(switch) > 1 else 0,
            }
        ]
        if switch
        else []
    )
    # Row 0 — slicers (and the metric switcher at the right).
    slicers = ([date] if date else []) + [d for d in dims if cardinality.get(d, 0) <= 12 and d != geo][
        : 4 - (1 if date else 0)
    ]
    slicer_width = 5 if parameters else 6
    for index, field in enumerate(slicers):
        style = "date_range" if field == date else "dropdown"
        add(
            f"slicer-{index}",
            "slicer",
            (index * slicer_width, 0, slicer_width, 3),
            {"category": [{"field": field}]},
            format={"slicer": {"style": style}},
        )
    if parameters:
        add(
            "metric",
            "parameter",
            (20, 0, 4, 3),
            {},
            format={"parameter": {"id": "metric", "style": "dropdown"}},
        )
    # Row 3 — KPI cards: the main measures, margin % and versus target % when the columns exist.
    calculations: list[dict[str, Any]] = []
    profit = next((m for m in measures if "profit" in m.lower()), None)
    revenue = next((m for m in measures if any(h in m.lower() for h in ("revenue", "sales", "amount"))), None)
    cards: list[dict[str, Any]] = []
    for field in measures:
        wells: dict[str, Any] = {"values": [{"field": field, "agg": agg(field)}]}
        target = _target_for(field, numeric)
        if target:
            wells["target"] = [{"field": target, "agg": agg(field)}]
        cards.append(wells)
    if profit and revenue and profit != revenue:
        calculations.append(
            {
                "id": "calc-margin",
                "name": "Profit margin",
                "expression": f"DIVIDE(SUM([{profit}]), SUM([{revenue}]))",
                "format": "percent",
                "datasetId": dataset_id,
            }
        )
        cards.insert(2, {"values": [{"field": "Profit margin", "calc": "calc-margin"}]})
    target_of_main = _target_for(main, numeric) if main else None
    if main and target_of_main:
        calculations.append(
            {
                "id": "calc-vs-target",
                "name": "Vs target",
                "expression": f"DIVIDE(SUM([{main}]), SUM([{target_of_main}])) - 1",
                "format": "percent",
                "datasetId": dataset_id,
            }
        )
        cards.insert(min(5, len(cards)), {"values": [{"field": "Vs target", "calc": "calc-vs-target"}]})
    # Six cards: keep the calculations, then averages (scores) over further sums.
    calc_cards = [c for c in cards if c["values"][0].get("calc")]
    plain = [c for c in cards if not c["values"][0].get("calc")]

    def named(card: dict[str, Any], hints: tuple[str, ...]) -> bool:
        return any(hint in card["values"][0]["field"].lower() for hint in hints)

    rest = plain[2:]
    rates = sorted(
        [c for c in rest if c["values"][0]["agg"] == "avg"],
        key=lambda c: not named(c, ("satisfaction", "score", "rating", "rate")),
    )
    volumes = sorted(
        [c for c in rest if c not in rates],
        key=lambda c: not named(c, ("unit", "qty", "quantity", "order", "volume", "count")),
    )
    chosen = plain[:2] + calc_cards[:1] + volumes[:1] + rates[:1] + calc_cards[1:2]
    chosen += [c for c in plain if c not in chosen]
    kpis = chosen[:6]
    width = 24 // max(len(kpis), 1) if kpis else 4
    for index, wells in enumerate(kpis):
        if date:
            wells = {**wells, "category": [{"field": date}]}
        add(
            f"kpi-{index}",
            "kpi",
            (index * width, 3, width, 5),
            wells,
            format={
                "kpi": {"grain": grain if grain in {"day", "week", "month", "quarter", "year"} else "month"}
            },
        )
    if not kpis:
        add("kpi-0", "card", (0, 3, 6, 5), {"values": [{"field": "*"}]})
    # Row 7 — trend and composition.
    # Entities measured over time (tickers, stores, sensors): one line each, when a level is tracked.
    entity = next((d for d in dims if 2 <= cardinality.get(d, 0) <= 8 and d != geo), None)
    if date and main and entity and agg(main) == "avg":
        add(
            "trend",
            "line",
            (0, 8, 16, 10),
            {
                "category": [{"field": date, "grain": grain}],
                "values": [{"field": main, "agg": "avg"}],
                "legend": [{"field": entity}],
            },
        )
    elif date and main:
        wells = {
            "category": [{"field": date, "grain": grain}],
            "values": [{"field": main, "agg": agg(main)}],
        }
        if second:
            wells["secondaryValues"] = [{"field": second, "agg": agg(second)}]
        add("trend", "combo" if second else "line", (0, 8, 16, 10), wells)
    elif main and second and not dims:
        # No dates and no groups: how two measures relate.
        add(
            "trend",
            "scatter",
            (0, 8, 16, 10),
            {"details": [], "values": [{"field": main, "agg": "sum"}, {"field": second, "agg": "sum"}]},
        )
    elif medium and main:
        add(
            "trend",
            "column",
            (0, 8, 16, 10),
            {"category": [{"field": medium[0]}], "values": [{"field": main, "agg": agg(main)}]},
        )
    # Parts of a whole only for added-up measures; averages are compared side by side instead.
    flow = next((m for m in measures if agg(m) == "sum"), None)
    if small and flow:
        add(
            "share",
            "donut",
            (16, 8, 8, 10),
            {"category": [{"field": small[0]}], "values": [{"field": flow, "agg": "sum"}]},
        )
    elif small and main:
        add(
            "share",
            "column",
            (16, 8, 8, 10),
            {"category": [{"field": small[0]}], "values": [{"field": main, "agg": agg(main)}]},
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
                "values": [
                    {"field": rank_by, "agg": agg(rank_by), **({"param": "metric"} if parameters else {})}
                ]
                if rank_by
                else [],
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
                "values": [{"field": main, "agg": agg(main)}] if main else [],
            },
            format={"conditional": {"rules": [{"kind": "scale", "min": "#eef2ff", "max": "#4f46e5"}]}},
        )
    if geo:
        add(
            "map",
            "map",
            (16, 18, 8, 12),
            {"category": [{"field": geo}], "values": [{"field": main, "agg": agg(main)}] if main else []},
        )
    elif medium[1:]:
        add(
            "ranking-2",
            "column",
            (16, 18, 8, 12),
            {
                "category": [{"field": medium[1]}],
                "values": [{"field": main, "agg": agg(main)}] if main else [],
            },
        )
    pages = [{"id": "page-1", "name": name, "visuals": visuals}]
    # Page 2 — a drill-through detail table (right-click a member → Drill through ▸ Details).
    detail_fields = [f for f in (ranking, geo, rows) if f][:3]
    if detail_fields and main:
        detail_values = [{"field": m, "agg": agg(m)} for m in measures[:4]]
        pages.append(
            {
                "id": "details",
                "name": "Details",
                "type": "drillthrough",
                "drillthroughFields": list(dict.fromkeys(detail_fields)),
                "visuals": [
                    {
                        "id": "detail-kpi",
                        "type": "multi_card",
                        "layout": {"x": 0, "y": 0, "w": 24, "h": 5, "z": 0},
                        "data": {"datasetId": dataset_id, "wells": {"values": detail_values}, "filters": []},
                    },
                    {
                        "id": "detail-table",
                        "type": "table",
                        "layout": {"x": 0, "y": 5, "w": 24, "h": 25, "z": 0},
                        "data": {
                            "datasetId": dataset_id,
                            "wells": {
                                "category": [{"field": d} for d in dims[:3]]
                                + ([{"field": date, "grain": grain}] if date else []),
                                "values": detail_values,
                            },
                            "filters": [],
                        },
                    },
                ],
            }
        )
    return {
        "theme": _theme_for([e.get("name", "") for e in profile.get("columnProfiles", [])]),
        "mode": "system",
        "calculations": calculations,
        "parameters": parameters,
        "pages": pages,
    }
