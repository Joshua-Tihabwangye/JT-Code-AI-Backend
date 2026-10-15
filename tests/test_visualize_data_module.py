"""Phase 5: measures library, relationships across datasets, snapshots, short insights, a chart's
one-line insight and dashboard exports (Excel, HTML, JSON, PDF)."""

from __future__ import annotations

import base64
import io
import zipfile

import pandas as pd
import pytest

from apps.visualize import dashboard_spec, themes, visual_plan, viz_engine
from tests.test_visualize_api import _client, _org, _sample
from tests.test_visualize_engine import _frame, _kinds
from tests.test_visualize_store import _dataset

BASE = "/api/v1/visualize"
# A 1×1 transparent PNG.
PNG = (
    "data:image/png;base64,"
    + base64.b64encode(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
        )
    ).decode()
)


def _items(response):
    body = response.json()
    return body["results"] if isinstance(body, dict) else body


@pytest.mark.django_db
def test_measures_library_is_shared_in_the_organization_and_checked():
    org, owner = _org("measures")
    api = _client(owner, org)
    dataset = _sample(api)
    created = api.post(
        f"{BASE}/measures/",
        {
            "name": "Margin",
            "expression": "DIVIDE(SUM([profit]), SUM([revenue]))",
            "format": "percent",
            "datasetId": dataset["id"],
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    assert _items(api.get(f"{BASE}/measures/", {"datasetId": dataset["id"]}))[0]["name"] == "Margin"
    bad = api.post(f"{BASE}/measures/", {"name": "Bad", "expression": "SUM("}, format="json")
    assert bad.status_code == 400 and "expression" in bad.json().get("details", bad.json())
    other_org, stranger = _org("measures-other")
    assert _items(_client(stranger, other_org).get(f"{BASE}/measures/")) == [], (
        "other organizations see nothing"
    )
    foreign = _sample(_client(stranger, other_org))
    blocked = api.post(
        f"{BASE}/measures/", {"name": "X", "expression": "1", "datasetId": foreign["id"]}, format="json"
    )
    assert blocked.status_code == 404


@pytest.mark.django_db
def test_slicers_filter_across_related_datasets():
    orders = _dataset(b"order,region,channel\n1,East,Online\n2,West,Shop\n3,East,Shop\n", "orders")
    goals = _dataset(b"region,goal\nEast,10\nWest,20\nNorth,30\n", "goals")
    org = orders.organization
    goals.organization = org
    goals.owner = orders.owner
    goals.save()
    api = _client(orders.owner, org)
    card = {
        "id": "goal",
        "type": "card",
        "data": {"datasetId": str(goals.id), "wells": {"values": [{"field": "goal"}]}},
    }
    link = [
        {
            "from": {"datasetId": str(orders.id), "field": "region"},
            "to": {"datasetId": str(goals.id), "field": "region"},
        }
    ]

    def goal(filters, relationships=link):
        response = api.post(
            f"{BASE}/visuals/",
            {"visuals": [card], "filters": filters, "relationships": relationships, "mode": "light"},
            format="json",
        )
        assert response.status_code == 200, response.content
        return response.json()["visuals"][0]["kpi"]["value"]

    assert goal([]) == 60
    assert goal([{"field": "channel", "op": "in", "value": ["Online"]}]) == 10, (
        "Online orders are all in East"
    )
    assert goal([{"field": "channel", "op": "in", "value": ["Shop"]}]) == 30, "East and West"
    assert goal([{"field": "channel", "op": "in", "value": ["Fax"]}]) is None, "no matching keys → nothing"
    assert goal([{"field": "channel", "op": "in", "value": ["Online"]}], relationships=[]) == 60
    with pytest.raises(dashboard_spec.SpecError, match="two different datasets"):
        dashboard_spec.clean_relationships(
            [{"from": {"datasetId": "a", "field": "x"}, "to": {"datasetId": "a", "field": "y"}}]
        )


def test_snapshots_keep_formatter_markers_but_drop_html():
    spec = dashboard_spec.clean_spec(
        {
            "pages": [
                {
                    "id": "p",
                    "visuals": [
                        {
                            "id": "s",
                            "type": "snapshot",
                            "format": {
                                "snapshot": {
                                    "option": {
                                        "tooltip": {
                                            "formatter": "<img src=x onerror=alert(1)>",
                                            "valueFormatter": {"$fn": "number"},
                                        },
                                        "yAxis": {"axisLabel": {"formatter": {"$fn": "compact"}}},
                                        "title": {"text": "Forecast", "link": "https://evil.example"},
                                        "series": [{"type": "line", "data": [1, 2, 3]}],
                                    },
                                    "takenAt": "2026-10-14",
                                }
                            },
                        }
                    ],
                }
            ]
        }
    )
    option = spec["pages"][0]["visuals"][0]["format"]["snapshot"]["option"]
    assert "formatter" not in option["tooltip"] and option["tooltip"]["valueFormatter"] == {"$fn": "number"}
    assert option["yAxis"]["axisLabel"]["formatter"] == {"$fn": "compact"} and "link" not in option["title"]
    with pytest.raises(dashboard_spec.SpecError, match="needs a chart"):
        dashboard_spec.clean_spec({"pages": [{"id": "p", "visuals": [{"id": "s", "type": "snapshot"}]}]})


def test_insights_have_short_headlines_and_charts_a_one_line_reading():
    frame = _frame("retail_sales")
    profile, kinds = viz_engine.d.column_kinds(frame)
    found = viz_engine.insights(viz_engine.d.typed(frame, kinds), profile, kinds)
    assert all(len(item["headline"].rstrip("…").split()) <= 6 for item in found)
    correlation = next(item for item in found if item["kind"] == "correlation")
    assert correlation["value"].startswith("r = ") and "_" not in correlation["headline"]

    kinds = _kinds("retail_sales")
    typed = viz_engine.d.typed(frame, kinds)

    def build(visual):
        queries = visual_plan.plan(visual, kinds, [], pd.Timestamp("2026-01-15").date())
        return viz_engine.vv.build(
            visual,
            queries,
            {role: viz_engine.vq.aggregate(typed, q, kinds) for role, q in queries.items()},
            viz_engine.vv.Ctx(themes.resolve("executive", "light")),
            world_names=[],
            continents={},
            geo_base=lambda scope: {},
        )

    bars = build(
        {
            "id": "b",
            "type": "column",
            "data": {"wells": {"category": [{"field": "region"}], "values": [{"field": "revenue"}]}},
        }
    )
    assert bars["insight"].startswith("West leads with") and "% of the total" in bars["insight"]
    assert len(bars["insight"].split()) <= 15
    trend = build(
        {
            "id": "t",
            "type": "line",
            "data": {
                "wells": {"category": [{"field": "date", "grain": "month"}], "values": [{"field": "revenue"}]}
            },
        }
    )
    assert trend["insight"].split()[0] in {"Up", "Down"} and "from Jan 2024 to Dec 2025" in trend["insight"]


@pytest.mark.django_db
def test_dashboard_exports():
    org, owner = _org("exports")
    api = _client(owner, org)
    dataset = _sample(api)
    dashboard = api.post(f"{BASE}/dashboards/auto/", {"datasetId": dataset["id"]}, format="json").json()

    def export(**body):
        return api.post(f"{BASE}/dashboards/{dashboard['id']}/export/", body, format="json")

    xlsx = export(format="xlsx")
    assert xlsx.status_code == 200, xlsx.content[:300]
    names = zipfile.ZipFile(io.BytesIO(xlsx.content)).namelist()
    assert any(name.startswith("xl/charts/") for name in names), "native Excel charts"
    html = export(format="html", mode="dark")
    text = html.content.decode()
    assert html.status_code == 200 and text.count('class="plot"') >= 4 and "echarts" in text
    assert "<script>alert" not in text
    spec = export(format="json")
    assert spec.status_code == 200 and b'"pages"' in spec.content
    pdf = export(
        format="pdf", images=[{"title": "Overview", "image": PNG}, {"title": "Page 2", "image": PNG}]
    )
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    remote = export(format="pdf", images=[{"title": "x", "image": "https://evil.example/a.png"}])
    assert remote.status_code == 400 and "not a PNG" in remote.content.decode()
    assert export(format="pdf", images=[]).status_code == 400
