"""Build dashboard picks charts that suit the data (aggregation, time grain, chart type, colours)."""

from __future__ import annotations

from typing import Any

from apps.visualize import autodash


def _column(name: str, kind: str, distinct: int, **stats: Any) -> dict[str, Any]:
    return {"name": name, "kind": kind, "distinct": distinct, "nulls": 0, **stats}


def _visuals(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {visual["id"]: visual for visual in spec["pages"][0]["visuals"]}


def test_stock_prices_are_averaged_volume_summed_one_line_per_ticker():
    profile = {
        "rows": 360,
        "columnProfiles": [
            _column("date", "datetime", 180, min="2024-01-02T00:00:00", max="2024-06-29T00:00:00"),
            _column("ticker", "categorical", 2),
            _column("open", "numeric", 360, min=90, max=130),
            _column("high", "numeric", 360, min=91, max=131),
            _column("low", "numeric", 360, min=89, max=129),
            _column("close", "numeric", 360, min=90, max=130),
            _column("volume", "numeric", 360, min=100_000, max=990_000),
        ],
    }
    spec = autodash.build("ds", profile, "Stock")
    visuals = _visuals(spec)
    trend = visuals["trend"]["data"]["wells"]
    assert visuals["trend"]["type"] == "line"
    assert trend["values"] == [{"field": "close", "agg": "avg"}]  # the closing price leads, averaged
    assert trend["legend"] == [{"field": "ticker"}]  # one line per ticker
    assert trend["category"][0]["grain"] == "week"  # six months of days → weekly points
    kpis = [visual["data"]["wells"]["values"][0] for key, visual in visuals.items() if key.startswith("kpi-")]
    assert {"field": "volume", "agg": "sum"} in kpis
    assert all(item["agg"] == "avg" for item in kpis if item["field"] != "volume")
    # Parts of a whole only for a summed measure.
    assert visuals["share"]["type"] == "donut"
    assert visuals["share"]["data"]["wells"]["values"] == [{"field": "volume", "agg": "sum"}]
    assert spec["theme"] == "corporate"


def test_two_measures_without_dates_or_groups_are_compared_in_a_scatter():
    profile = {
        "rows": 200,
        "columnProfiles": [
            _column("spend", "numeric", 200, min=1, max=900),
            _column("conversions", "numeric", 150, min=0, max=80),
        ],
    }
    spec = autodash.build("ds", profile)
    trend = _visuals(spec)["trend"]
    assert trend["type"] == "scatter"
    assert [value["field"] for value in trend["data"]["wells"]["values"]] == ["spend", "conversions"]
    assert spec["theme"] == "vivid"


def test_ratios_between_zero_and_one_are_averaged():
    assert autodash._agg("churn_flag", {"churn_flag": {"min": 0, "max": 1}}) == "avg"
    assert autodash._agg("units_sold") == "sum"
    assert autodash._agg("closing_price") == "avg"
