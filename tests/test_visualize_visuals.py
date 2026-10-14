"""Dashboard visuals are built in Python from aggregated data (engine, pandas path)."""

from __future__ import annotations

import base64
import datetime as dt
import json

import pytest

from apps.visualize import samples, themes, visual_plan, viz_engine
from tests.test_visualize_engine import LIMITS, _frame, _kinds

TODAY = dt.date(2026, 1, 15)


def _visual(vid: str, vtype: str, **wells) -> dict:
    fmt = wells.pop("format", {})
    return {"id": vid, "type": vtype, "data": {"wells": wells}, "format": fmt}


VISUALS = [
    _visual("rev", "card", values=[{"field": "revenue"}]),
    _visual("multi", "multi_card", values=[{"field": "revenue"}, {"field": "profit"}, {"field": "units"}]),
    _visual(
        "kpi",
        "kpi",
        values=[{"field": "revenue"}],
        target=[{"field": "target_revenue"}],
        category=[{"field": "date"}],
    ),
    _visual("gauge", "gauge", values=[{"field": "revenue"}], target=[{"field": "target_revenue"}]),
    _visual("bar", "bar", category=[{"field": "channel"}], values=[]),
    _visual("col", "column", category=[{"field": "region"}], values=[{"field": "revenue"}]),
    _visual(
        "stack",
        "column",
        category=[{"field": "region"}],
        legend=[{"field": "channel"}],
        values=[{"field": "revenue"}],
        format={"bar": {"stack": "stacked"}},
    ),
    _visual(
        "line",
        "line",
        category=[{"field": "date", "grain": "month"}],
        values=[{"field": "revenue"}, {"field": "profit"}],
    ),
    _visual(
        "area",
        "area",
        category=[{"field": "date", "grain": "quarter"}],
        legend=[{"field": "channel"}],
        values=[{"field": "units"}],
    ),
    _visual(
        "combo",
        "combo",
        category=[{"field": "date", "grain": "month"}],
        values=[{"field": "revenue"}],
        secondaryValues=[{"field": "units"}],
    ),
    _visual("donut", "donut", category=[{"field": "product"}], values=[{"field": "revenue"}]),
    _visual("tree", "treemap", category=[{"field": "country"}], values=[{"field": "revenue"}]),
    _visual("funnel", "funnel", category=[{"field": "channel"}], values=[{"field": "units"}]),
    _visual(
        "water", "waterfall", category=[{"field": "date", "grain": "quarter"}], values=[{"field": "profit"}]
    ),
    _visual(
        "scatter",
        "scatter",
        details=[{"field": "product"}],
        values=[{"field": "revenue"}, {"field": "profit"}, {"field": "units"}],
    ),
    _visual("map", "map", category=[{"field": "country"}], values=[{"field": "revenue"}]),
    _visual(
        "table", "table", category=[{"field": "product"}], values=[{"field": "revenue"}, {"field": "profit"}]
    ),
    _visual(
        "matrix",
        "matrix",
        rows=[{"field": "product"}],
        columns=[{"field": "region"}],
        values=[{"field": "revenue"}],
    ),
    _visual("slicer", "slicer", category=[{"field": "channel"}]),
    _visual("dates", "slicer", category=[{"field": "date"}]),
    _visual(
        "share",
        "column",
        category=[{"field": "region"}],
        values=[{"field": "revenue", "showValueAs": "pct_total"}],
    ),
]


@pytest.fixture(scope="module")
def built() -> dict[str, dict]:
    kinds = _kinds("retail_sales")
    items = []
    for visual in VISUALS:
        queries = visual_plan.plan(visual, kinds, [], TODAY)
        items.append({"visual": visual, "queries": queries})
    output = viz_engine.dashboard_visuals(
        {
            "data": base64.b64encode(samples.sample_csv("retail_sales").encode()).decode(),
            "theme": themes.resolve("executive", "dark"),
            "items": items,
        },
        LIMITS,
    )
    return {v["id"]: v for v in output["visuals"]}


@pytest.mark.parametrize("visual", VISUALS, ids=lambda v: v["id"])
def test_every_visual_type_builds(built, visual):
    out = built[visual["id"]]
    assert out["status"] == "ok", out
    assert out["title"]
    json.dumps(out, allow_nan=False)  # plain JSON for the browser


def test_cards_kpis_and_gauges_carry_formatted_numbers(built):
    assert built["rev"]["kpi"]["display"].endswith("M")
    assert [c["label"] for c in built["multi"]["cards"]] == ["Total Revenue", "Total Profit", "Total Units"]
    kpi = built["kpi"]["kpi"]
    assert kpi["delta"]["display"][0] in "▲▼■" and kpi["delta"]["label"] == "vs prev. month"
    assert len(kpi["sparkline"]) == 12 and kpi["target"]["progressDisplay"].endswith("%")
    assert built["gauge"]["option"]["series"][0]["type"] == "gauge"


def test_chart_defaults(built):
    bar = built["bar"]["option"]
    series = bar["series"][0]
    assert bar["yAxis"]["type"] == "category" and series["barMaxWidth"] == 48, (
        "bar = horizontal, bars capped at 48 px"
    )
    values = series["data"]
    assert values == sorted(values, reverse=True), "categorical bars are sorted by value"
    assert series["label"]["show"] is True, "few bars → data labels on"
    assert "legend" not in bar, "no legend for a single series"
    assert built["bar"]["title"] == "Count by channel"
    line = built["line"]["option"]
    assert line["xAxis"]["data"][0].startswith("Jan") and "legend" in line, (
        "dates are chronological; two series get a legend"
    )
    donut = built["donut"]["option"]["series"][0]
    assert len(donut["data"]) <= 7 and donut["label"]["formatter"] == "{b} · {d}%"
    assert built["share"]["option"]["series"][0]["label"]["formatter"]["$fn"] == "percent"


def test_category_colours_are_consistent_and_clicks_map_back_to_values(built):
    stacked = built["stack"]
    assert stacked["selectors"]["series"][0]["field"] == "channel"
    assert stacked["selectors"]["category"][0] == {
        "field": "region",
        "op": "in",
        "value": [stacked["data"]["rows"][0][0]],
    }
    month = built["line"]["selectors"]["category"][0]
    assert month["op"] == "between" and month["value"] == ["2024-01-01T00:00:00", "2024-01-31T23:59:59"]
    colours = {s["name"]: s["itemStyle"]["color"] for s in built["area"]["option"]["series"]}
    funnel = {d["name"]: d["itemStyle"]["color"] for d in built["funnel"]["option"]["series"][0]["data"]}
    assert all(colours[name] == funnel[name] for name in funnel if name in colours), (
        "a channel has one colour everywhere"
    )


def test_tables_matrices_maps_and_slicers(built):
    table = built["table"]["table"]
    assert [c["label"] for c in table["columns"]] == ["Product", "Total Revenue", "Total Profit"]
    assert table["totals"]["m0"] > 0 and table["bars"]["m0"] > 0
    matrix = built["matrix"]["table"]
    assert matrix["matrix"] and matrix["rows"][0]["cells"][0]["shade"] > 0
    assert built["map"]["option"]["series"][0]["center"] == [20, 2], "African countries zoom to Africa"
    assert {o["label"] for o in built["slicer"]["slicer"]["options"]} == {"Online", "Retail store", "Partner"}
    assert built["dates"]["slicer"]["range"]["min"].startswith("2024")


def test_filters_and_highlight_are_applied():
    kinds = _kinds("retail_sales")
    visual = _visual("col", "column", category=[{"field": "channel"}], values=[{"field": "revenue"}])
    visual["data"]["highlight"] = [{"field": "region", "op": "in", "value": ["East"]}]
    filters = [{"field": "date", "op": "relative", "value": {"unit": "year", "mode": "last", "n": 1}}]
    queries = visual_plan.plan(visual, kinds, filters, TODAY)
    assert queries["main"]["filters"][0]["op"] == "between" and "highlight" in queries
    frame = _frame("retail_sales")
    typed = viz_engine.d.typed(frame, kinds)
    out = viz_engine.vv.build(
        visual,
        queries,
        {role: viz_engine.vq.aggregate(typed, q, kinds) for role, q in queries.items()},
        viz_engine.vv.Ctx(themes.resolve("vivid", "light")),
        world_names=viz_engine.c.world_names,
        continents=viz_engine.c.world_continents,
        geo_base=lambda scope: {},
    )
    series = out["option"]["series"]
    assert len(series) == 2 and series[1]["barGap"] == "-100%", "the selection is drawn over the faded total"
    assert sum(v for v in series[1]["data"] if v) < sum(v for v in series[0]["data"] if v)


def test_gallery_charts_colours_and_legend_position():
    kinds = _kinds("retail_sales")
    violin = _visual(
        "violin",
        "chart",
        format={"chart": {"chartType": "violin", "fields": {"y": "revenue", "x": "channel"}}},
    )
    coloured = _visual(
        "coloured",
        "line",
        category=[{"field": "date", "grain": "month"}],
        values=[{"field": "revenue"}, {"field": "profit"}],
        format={"colors": {"series": ["#112233", "not-a-colour"]}, "legend": {"position": "bottom"}},
    )
    items = [{"visual": v, "queries": visual_plan.plan(v, kinds, [], TODAY)} for v in (violin, coloured)]
    assert items[0]["queries"]["main"]["rows"] and items[0]["queries"]["main"]["columns"] == [
        "revenue",
        "channel",
    ]
    out = viz_engine.dashboard_visuals(
        {
            "data": base64.b64encode(samples.sample_csv("retail_sales").encode()).decode(),
            "theme": themes.resolve("vivid", "dark"),
            "items": items,
        },
        LIMITS,
    )
    chart, line = out["visuals"]
    assert chart["status"] == "ok" and chart["option"]["backgroundColor"] == "transparent", chart
    assert chart["title"] and chart["data"]["rows"]
    assert line["option"]["series"][0]["itemStyle"]["color"] == "#112233"
    assert line["option"]["legend"]["bottom"] == 0 and line["option"]["grid"]["bottom"] >= 28
    with pytest.raises(visual_plan.PlanError):
        visual_plan.plan(
            _visual("bad", "chart", format={"chart": {"chartType": "violin", "fields": {"y": "nope"}}}),
            kinds,
            [],
            TODAY,
        )
