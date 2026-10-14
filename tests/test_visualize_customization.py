"""Dashboard customization (Phase 4): calculated measures and parameters, visual calculations,
conditional formatting, analytics lines, small multiples, Python visuals and the spec additions."""

from __future__ import annotations

import datetime as dt
import json
import math

import numpy as np
import pandas as pd
import pytest

from apps.visualize import (
    dashboard_spec,
    samples,
    store,
    themes,
    visual_plan,
    viz_engine,
    viz_expr,
    viz_python,
)
from tests.test_visualize_api import _client, _org, _sample
from tests.test_visualize_engine import _frame, _kinds
from tests.test_visualize_store import _dataset, _normal, _pandas

TODAY = dt.date(2026, 1, 15)
BASE = "/api/v1/visualize"


def _visual(vid: str, vtype: str, fmt: dict | None = None, **wells) -> dict:
    return {"id": vid, "type": vtype, "data": {"wells": wells}, "format": fmt or {}}


def _build(visual: dict, context: dict | None = None) -> dict:
    kinds = _kinds("retail_sales")
    queries = visual_plan.plan(visual, kinds, [], TODAY, context)
    typed = viz_engine.d.typed(_frame("retail_sales"), kinds)
    return viz_engine.vv.build(
        visual,
        queries,
        {role: viz_engine.vq.aggregate(typed, q, kinds) for role, q in queries.items()},
        viz_engine.vv.Ctx(themes.resolve("executive", "light")),
        world_names=viz_engine.c.world_names,
        continents=viz_engine.c.world_continents,
        geo_base=lambda scope: {},
        kinds=kinds,
    )


# --- The expression language --------------------------------------------------------------------


def test_expressions_parse_resolve_and_evaluate():
    tree = viz_expr.parse("DIVIDE(SUM([profit]), SUM([revenue])) * 100")
    assert viz_expr.aggregates(tree) == [("sum", "profit"), ("sum", "revenue")]
    leaves = {("sum", "profit"): [10.0, 5.0, math.nan], ("sum", "revenue"): [40.0, 0.0, 10.0]}
    assert list(np.round(viz_expr.evaluate(tree, leaves, 3), 6)[:1]) == [25.0]
    result = viz_expr.evaluate(tree, leaves, 3)
    assert math.isnan(result[1]) and math.isnan(result[2]), "DIVIDE by zero and blanks give blank"

    priced = viz_expr.resolve(viz_expr.parse("SUM([revenue]) * (1 + [Price change])"), {"price change": 0.1})
    assert viz_expr.evaluate(priced, {("sum", "revenue"): [200.0]}, 1)[0] == pytest.approx(220.0)

    calcs = {"Margin": "DIVIDE(SUM([profit]), SUM([revenue]))", "Margin pct": "[Margin] * 100"}
    nested = viz_expr.resolve(viz_expr.parse("ROUND([Margin pct], 1)"), {}, calcs)
    assert viz_expr.aggregates(nested) == [("sum", "profit"), ("sum", "revenue")]
    flags = viz_expr.parse("IF(AND(COUNTROWS() > 2, NOT(MIN([units]) < 0)), 1, 0)")
    out = viz_expr.evaluate(flags, {("count", "*"): [3.0, 1.0], ("min", "units"): [1.0, 1.0]}, 2)
    assert list(out) == [1.0, 0.0]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('__import__("os")', "Unexpected character"),
        ("open([x])", "Unknown function"),
        ("SUM(1)", "needs a field"),
        ("SUM([a]); DROP TABLE x", "Unexpected character"),
        ("SUM([a]) +", "ends too early"),
        ("(" * 40 + "1" + ")" * 40, "nested too deeply"),
        ("1+" * 600 + "1", "limited to"),
        ("[a]", "Unknown name"),
        ("", "Write an expression"),
    ],
)
def test_bad_expressions_are_rejected(text, message):
    with pytest.raises(viz_expr.ExprError, match=message):
        viz_expr.resolve(viz_expr.parse(text))


def test_calculations_cannot_refer_to_themselves():
    with pytest.raises(viz_expr.ExprError, match="refers to itself"):
        viz_expr.resolve(viz_expr.parse("[A]"), {}, {"A": "[B] + 1", "B": "[A]"})


# --- Calculated measures in visuals ---------------------------------------------------------------


def test_calculated_measures_become_plain_aggregates_plus_an_output():
    kinds = _kinds("retail_sales")
    query = visual_plan.clean_query(
        {
            "groupBy": [{"field": "region"}],
            "measures": [
                {"field": "revenue"},
                {"field": "Margin", "expr": "DIVIDE(SUM([profit]), SUM([revenue]))", "format": "percent"},
            ],
        },
        kinds,
        TODAY,
    )
    assert [m["id"] for m in query["measures"]] == ["m0", "m1_0", "m1_1"]
    assert [m["agg"] for m in query["output"]] == ["sum", "calc"]
    with pytest.raises(visual_plan.PlanError, match="numeric field"):
        visual_plan.clean_query({"measures": [{"field": "x", "expr": "SUM([region])"}]}, kinds, TODAY)
    # A parameter change keeps the database part (and its cache key) the same.
    first = visual_plan.clean_query(
        {"measures": [{"field": "P", "expr": "SUM([revenue]) * [Lift]"}]},
        kinds,
        TODAY,
        {"parameters": {"Lift": 1}},
    )
    second = visual_plan.clean_query(
        {"measures": [{"field": "P", "expr": "SUM([revenue]) * [Lift]"}]},
        kinds,
        TODAY,
        {"parameters": {"Lift": 2}},
    )
    assert first["output"] != second["output"]
    assert visual_plan.query_key("t", first) == visual_plan.query_key("t", second)


def test_calculated_measures_draw_as_percentages_and_follow_parameters():
    margin = _visual(
        "m",
        "column",
        category=[{"field": "region"}],
        values=[{"field": "Margin", "expr": "DIVIDE(SUM([profit]), SUM([revenue]))", "format": "percent"}],
    )
    out = _build(margin)
    assert out["status"] == "ok", out
    data = out["option"]["series"][0]["data"]
    assert all(0 < v < 1 for v in data), data
    assert out["option"]["series"][0]["label"]["formatter"]["$fn"] == "percent"
    assert out["title"] == "Margin by region"

    card = _visual(
        "c", "card", values=[{"field": "Projected", "expr": "SUM([revenue]) * (1 + [Price change])"}]
    )
    base = _build(card, {"parameters": {"Price change": 0}})["kpi"]["value"]
    raised = _build(card, {"parameters": {"Price change": 0.1}})["kpi"]["value"]
    assert raised == pytest.approx(base * 1.1)


@pytest.mark.django_db
def test_calculations_agree_between_postgres_and_pandas():
    csv = samples.sample_csv("retail_sales").encode()
    record = store.ensure_table(_dataset(csv, "calc-store"))
    frame, kinds = _pandas(csv)
    query = visual_plan.clean_query(
        {
            "groupBy": [{"field": "channel"}],
            "measures": [{"field": "AOV", "expr": "DIVIDE(SUM([revenue]), COUNTROWS())"}, {"field": "units"}],
        },
        kinds,
        TODAY,
    )
    materialize = viz_engine.vv._materialize
    _, via_sql = materialize(query, store.run_query(record, query))
    _, via_pandas = materialize(query, viz_engine.vq.aggregate(frame, query, kinds))
    assert via_sql["columns"] == ["g0", "m0", "m1"]
    assert _normal(via_sql["rows"]) == _normal(via_pandas["rows"])


def test_moving_average_and_year_over_year():
    def line(mode: str, **extra) -> list:
        visual = _visual(
            "l",
            "line",
            category=[{"field": "date", "grain": "month"}],
            values=[{"field": "revenue", "showValueAs": mode, **extra}],
        )
        return _build(visual)["option"]["series"][0]["data"]

    plain = line("none")
    moving = line("moving_avg", window=3)
    assert moving[2] == pytest.approx(sum(plain[:3]) / 3, rel=1e-6)
    assert moving[0] == pytest.approx(plain[0])
    yoy = line("yoy_pct")
    assert yoy[0] is None and yoy[12] == pytest.approx((plain[12] - plain[0]) / plain[0], rel=1e-6)


# --- Conditional formatting, analytics, small multiples -----------------------------------------------


def test_conditional_formatting_in_tables_matrices_charts_and_cards():
    rules = {
        "rules": [
            {"measure": "m0", "kind": "scale", "min": "#FFFFFF", "max": "#1E3A8A"},
            {"measure": "m0", "kind": "icons"},
            {
                "measure": "m1",
                "kind": "rules",
                "target": "font",
                "rules": [{"op": "lt", "value": 0, "color": "#DC2626"}],
            },
        ]
    }
    table = _build(
        _visual(
            "t",
            "table",
            {"conditional": rules},
            category=[{"field": "product"}],
            values=[{"field": "revenue"}, {"field": "profit"}],
        )
    )["table"]
    styles = [row["_style"]["m0"] for row in table["rows"]]
    assert styles[0]["bg"] == "#1E3A8A" and styles[-1]["bg"] == "#FFFFFF", "sorted by revenue: high → low"
    assert styles[0]["icon"] == "▲" and styles[-1]["icon"] == "▼"
    assert styles[0]["color"] == "#FFFFFF" and styles[-1]["color"] == "#0F172A", (
        "readable text on any background"
    )

    matrix = _build(
        _visual(
            "x",
            "matrix",
            {
                "conditional": {
                    "rules": [{"kind": "rules", "rules": [{"op": "gt", "value": 100000, "color": "#123456"}]}]
                }
            },
            rows=[{"field": "product"}],
            columns=[{"field": "region"}],
            values=[{"field": "revenue"}],
        )
    )["table"]
    cells = [cell for row in matrix["rows"] for cell in row["cells"]]
    assert any(cell.get("style", {}).get("bg") == "#123456" for cell in cells)
    assert all(cell["shade"] == 0 for cell in cells), "rules replace the default heat shading"

    column = _build(
        _visual(
            "c",
            "column",
            {
                "conditional": {
                    "rules": [{"kind": "rules", "rules": [{"op": "gte", "value": 1.2e6, "color": "#16A34A"}]}]
                }
            },
            category=[{"field": "region"}],
            values=[{"field": "revenue"}],
        )
    )
    data = column["option"]["series"][0]["data"]
    assert isinstance(data[0], dict) and data[0]["itemStyle"]["color"] == "#16A34A"
    assert not isinstance(data[-1], dict), "bars that match no rule keep the series colour"

    card = _build(
        _visual(
            "k",
            "card",
            {
                "conditional": {
                    "rules": [
                        {
                            "kind": "rules",
                            "target": "font",
                            "rules": [{"op": "gt", "value": 0, "color": "#0000FF"}],
                        }
                    ]
                }
            },
            values=[{"field": "revenue"}],
        )
    )
    assert card["kpi"]["color"] == "#0000FF"


def test_analytics_lines_trend_and_forecast():
    lines = {
        "lines": [
            {"kind": "average"},
            {"kind": "constant", "value": 250000, "label": "Goal {x}"},
            {"kind": "percentile", "percentile": 90},
            {"kind": "trend"},
            {"kind": "forecast", "horizon": 3},
        ]
    }
    out = _build(
        _visual(
            "a",
            "line",
            {"analytics": lines},
            category=[{"field": "date", "grain": "month"}],
            values=[{"field": "revenue"}],
        )
    )
    option = out["option"]
    marks = option["series"][0]["markLine"]["data"]
    assert [m["name"] for m in marks] == ["Average", "Goal x", "P90"], "braces are stripped from labels"
    names = [s["name"] for s in option["series"]]
    assert "Trend" in names and "Forecast" in names
    assert len(option["xAxis"]["data"]) == 24 + 3 and option["xAxis"]["data"][-1] == "Mar 2026"
    forecast = next(s for s in option["series"] if s["name"] == "Forecast")
    assert forecast["data"][22] is None and forecast["data"][23] is not None and len(forecast["data"]) == 27
    assert "Forecast: 3 periods" in out["notes"]
    json.dumps(out, allow_nan=False)

    scatter = _build(
        _visual(
            "s",
            "scatter",
            {"analytics": {"lines": [{"kind": "trend"}, {"kind": "median"}]}},
            details=[{"field": "product"}],
            values=[{"field": "revenue"}, {"field": "profit"}],
        )
    )
    assert scatter["option"]["series"][1]["type"] == "line" and scatter["option"]["series"][0]["markLine"]


def test_small_multiples_share_one_scale():
    out = _build(
        _visual(
            "sm",
            "column",
            category=[{"field": "region"}],
            values=[{"field": "revenue"}],
            smallMultiples=[{"field": "channel"}],
        )
    )
    option = out["option"]
    assert len(option["grid"]) == len(option["series"]) == 3
    assert len({axis["max"] for axis in option["yAxis"]}) == 1, "one shared scale"
    assert out["selectors"]["series"][0]["field"] == "channel"
    assert out["selectors"]["category"][0]["field"] == "region"


# --- Python visuals --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "import os\nresult = 1",
        "from subprocess import run\nresult = 1",
        "result = ().__class__.__bases__[0].__subclasses__()",
        "result = open('/etc/passwd').read()",
        "result = eval('1+1')",
        "result = getattr(dataset, 'shape')",
        "result = pd.read_csv('/etc/passwd')",
        "result = dataset.query('revenue > 0')",
        "result = '{0.__class__}'.format(1)",
        "x = vars()\nresult = 1",
        "class A: pass\nresult = 1",
        "global x\nresult = 1",
        "result = np.load('x.npy')",
        "def f(_x): return _x\nresult = 1",
        "result = dataset['__dict__']",
    ],
)
def test_unsafe_python_is_rejected_before_it_runs(code):
    assert viz_python.validate(code), code
    with pytest.raises(viz_python.PythonVisualError):
        viz_python.run(code, pd.DataFrame())


def test_python_cannot_reach_system_modules_through_attributes():
    # These paths pass the static checks; the runtime attribute check stops them.
    with pytest.raises(viz_python.PythonVisualError, match="module 'platform'"):
        viz_python.run("import pandas\nresult = pandas.compat.platform.node()", pd.DataFrame())
    with pytest.raises(viz_python.PythonVisualError, match="module 'warnings'"):
        viz_python.run("import numpy\nresult = numpy.f2py.warnings", pd.DataFrame())


def test_python_visuals_draw_charts_tables_and_numbers():
    fields = [{"field": "revenue"}, {"field": "region"}]
    chart = _build(
        _visual(
            "py",
            "python",
            {
                "python": {
                    "code": (
                        "import numpy as np\n"
                        "totals = dataset.groupby('region')['revenue'].sum().sort_values()\n"
                        "print('regions', len(totals))\n"
                        "result = {'xAxis': {'type': 'category', 'data': list(totals.index)},"
                        " 'yAxis': {'type': 'value'},"
                        " 'tooltip': {'formatter': '<img src=x onerror=alert(1)>'},"
                        " 'title': {'text': 'T', 'link': 'https://evil.example'},"
                        " 'series': [{'type': 'bar', 'data': totals.round(2).tolist(), 'symbol': 'image://https://x'}]}"
                    )
                }
            },
            values=fields,
        )
    )
    assert chart["status"] == "ok", chart
    option = chart["option"]
    assert "formatter" not in option["tooltip"] and "link" not in option["title"]
    assert option["series"][0]["symbol"] == "" and len(option["series"][0]["data"]) == 5
    assert chart["console"].startswith("regions 5")

    table = _build(
        _visual(
            "py2",
            "python",
            {"python": {"code": "result = dataset.groupby('region')['revenue'].sum()"}},
            values=fields,
        )
    )
    assert [c["label"] for c in table["table"]["columns"]] == ["region", "revenue"]
    number = _build(
        _visual("py3", "python", {"python": {"code": "result = dataset['revenue'].mean()"}}, values=fields)
    )
    assert number["kpi"]["value"] > 0
    failing = _build(_visual("py4", "python", {"python": {"code": "result = 1 / 0"}}, values=fields))
    assert failing["status"] == "error" and "ZeroDivisionError" in failing["message"]
    wrong = _build(_visual("py5", "python", {"python": {"code": "result = 'text'"}}, values=fields))
    assert wrong["status"] == "error" and "ECharts option" in wrong["message"]


# --- Spec additions -----------------------------------------------------------------------------------------


def test_spec_keeps_calculations_parameters_bookmarks_and_mobile_layout():
    spec = dashboard_spec.clean_spec(
        {
            "calculations": [
                {
                    "id": "c1",
                    "name": "Margin",
                    "expression": "DIVIDE(SUM([profit]), SUM([revenue]))",
                    "format": "percent",
                }
            ],
            "parameters": [
                {
                    "id": "p1",
                    "name": "Price change",
                    "kind": "whatif",
                    "min": -0.5,
                    "max": 0.5,
                    "step": 0.05,
                    "default": 9,
                },
                {
                    "id": "p2",
                    "name": "Metric",
                    "kind": "fields",
                    "options": [{"field": "revenue"}, {"field": "profit"}],
                },
            ],
            "bookmarks": [
                {
                    "id": "b1",
                    "name": "East only",
                    "pageId": "p",
                    "state": {"slicers": {"s": {"values": ["East"]}}},
                }
            ],
            "pages": [
                {
                    "id": "p",
                    "visuals": [
                        _visual("v1", "parameter", {"parameter": {"id": "p1"}}),
                        {
                            **_visual(
                                "v2",
                                "column",
                                category=[{"field": "region"}],
                                values=[{"field": "revenue", "param": "p2"}],
                            )
                        },
                    ],
                    "mobileLayout": [
                        {"visualId": "v2", "y": 1, "h": 8},
                        {"visualId": "gone", "y": 0, "h": 4},
                        {"visualId": "v1", "y": 0},
                    ],
                }
            ],
        }
    )
    assert spec["calculations"][0]["format"] == "percent"
    assert spec["parameters"][0]["default"] == 0.5, "the default is kept inside the range"
    assert spec["parameters"][1]["options"][1] == {"field": "profit"}
    assert spec["bookmarks"][0]["state"]["slicers"]["s"]["values"] == ["East"]
    assert [m["visualId"] for m in spec["pages"][0]["mobileLayout"]] == ["v1", "v2"]
    assert spec["pages"][0]["visuals"][1]["data"]["wells"]["values"][0]["param"] == "p2"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"calculations": [{"id": "c", "name": "X", "expression": "SUM("}]}, "X:"),
        (
            {
                "calculations": [
                    {"id": "a", "name": "X", "expression": "1"},
                    {"id": "b", "name": "x", "expression": "2"},
                ]
            },
            "Two calculations",
        ),
        ({"parameters": [{"id": "p", "name": "P", "min": 5, "max": 1}]}, "maximum"),
        ({"parameters": [{"id": "p", "name": "P", "kind": "fields", "options": []}]}, "at least one field"),
        (
            {"pages": [{"id": "p", "visuals": [_visual("py", "python", {"python": {"code": "import os"}})]}]},
            "Python",
        ),
    ],
)
def test_bad_spec_additions_are_rejected(raw, message):
    with pytest.raises(dashboard_spec.SpecError, match=message):
        dashboard_spec.clean_spec(raw)


def test_personal_state_keeps_bookmarks_and_parameter_values():
    state = dashboard_spec.clean_personal_state(
        {
            "bookmarks": [{"id": "mine", "name": "Mine", "pageId": "p", "state": {}}],
            "defaultBookmark": "mine",
            "parameters": {"Price change": 0.2, "Metric": "1", "bad": {"x": 1}},
        }
    )
    assert state["bookmarks"][0]["id"] == "mine" and state["defaultBookmark"] == "mine"
    assert state["parameters"] == {"Price change": 0.2, "Metric": "1"}


@pytest.mark.django_db
def test_visuals_api_evaluates_calculations_with_parameters_and_isolates_python():
    org, owner = _org("dash-calc")
    api = _client(owner, org)
    dataset = _sample(api)
    wells = {"values": [{"field": "Projected", "expr": "SUM([revenue]) * (1 + [Lift]) - [Base]"}]}
    visuals = [
        {"id": "card", "type": "card", "data": {"datasetId": dataset["id"], "wells": wells}},
        {"id": "param", "type": "parameter", "data": {"datasetId": "", "wells": {}}},
        {
            "id": "py",
            "type": "python",
            "data": {"datasetId": dataset["id"], "wells": {"values": [{"field": "revenue"}]}},
            "format": {"python": {"code": "result = float(dataset['revenue'].sum())"}},
        },
    ]

    def draw(lift: float) -> dict:
        response = api.post(
            f"{BASE}/visuals/",
            {
                "visuals": visuals,
                "mode": "light",
                "parameters": {"Lift": lift},
                "calculations": [{"name": "Base", "expression": "0"}],
            },
            format="json",
        )
        assert response.status_code == 200, response.content
        return {v["id"]: v for v in response.json()["visuals"]} | {"meta": response.json()["meta"]}

    first, second = draw(0), draw(0.5)
    assert first["param"]["status"] == "ok"
    assert second["card"]["kpi"]["value"] == pytest.approx(first["card"]["kpi"]["value"] * 1.5)
    assert first["py"]["kpi"]["value"] == pytest.approx(first["card"]["kpi"]["value"])
    assert first["meta"]["engineRuns"] == 2, "the Python visual runs in its own engine process"
    assert second["meta"]["cachedQueries"] >= 1, "a new parameter value reuses the aggregated rows"
    bad = api.post(
        f"{BASE}/visuals/",
        {"visuals": [{**visuals[2], "format": {"python": {"code": "import os"}}}], "mode": "light"},
        format="json",
    )
    assert bad.status_code == 400 and "Python" in bad.content.decode()
