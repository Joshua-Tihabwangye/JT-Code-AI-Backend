"""Applied steps (Power Query style): every transform, validation, previews and the API."""

from __future__ import annotations

import io

import pandas as pd
import pytest

from apps.visualize import samples, viz_steps
from tests.test_visualize_api import _client, _org, _sample

BASE = "/api/v1/visualize"


@pytest.fixture(scope="module")
def retail() -> pd.DataFrame:
    frame = pd.read_csv(io.StringIO(samples.sample_csv("retail_sales")))
    frame["date"] = pd.to_datetime(frame["date"])
    return frame


def run(frame, *steps, others=None, upto=None):
    return viz_steps.run(frame, viz_steps.validate(list(steps)), others or {}, {}, upto)


def test_cleaning_steps(retail):
    messy = pd.DataFrame(
        {
            "name": ["  Ann ", "Bob", "Bob", None, "cid  smith"],
            "amount": ["1,200", "50", "50", "", "7"],
            "city": ["Kampala", "N/A", "N/A", "Gulu", None],
        }
    )
    out = run(
        messy,
        {"op": "remove_duplicates"},
        {"op": "trim"},
        {"op": "text_case", "column": "name", "case": "title"},
        {"op": "change_type", "column": "amount", "type": "number"},
        {"op": "replace_values", "column": "city", "find": "N/A", "replace": "Unknown"},
        {"op": "fill_blanks", "column": "amount", "strategy": "zero"},
        {"op": "fill_blanks", "column": "city", "strategy": "value", "value": "Unknown"},
        {"op": "rename", "column": "amount", "name": "Amount"},
        {"op": "remove_blank_rows", "columns": ["name"]},
    )
    assert out["name"].tolist() == ["Ann", "Bob", "Cid Smith"]
    assert out["Amount"].tolist() == [1200.0, 50.0, 7.0]
    assert out["city"].tolist() == ["Kampala", "Unknown", "Unknown"]
    numbers = pd.DataFrame({"amount": [1.0, None, 3.0]})
    assert run(numbers, {"op": "replace_values", "column": "amount", "find": "N/A"}).equals(numbers), (
        "text in a number column was already read as blank"
    )
    assert (
        run(numbers, {"op": "replace_values", "column": "amount", "find": "3", "replace": "30"})[
            "amount"
        ].tolist()[2]
        == 30
    )
    with pytest.raises(viz_steps.StepError, match="replace with a number"):
        run(numbers, {"op": "replace_values", "column": "amount", "find": "3", "replace": "lots"})


def test_shaping_steps(retail):
    out = run(
        retail,
        {
            "op": "filter_rows",
            "conditions": [
                {"field": "channel", "op": "eq", "value": "Online"},
                {"field": "units", "op": "gt", "value": 3},
            ],
        },
        {"op": "calculated_column", "name": "margin", "expression": "DIVIDE([profit], [revenue])"},
        {
            "op": "conditional_column",
            "name": "size",
            "rules": [{"field": "revenue", "op": "gte", "value": 2000, "then": "Large"}],
            "else": "Small",
        },
        {"op": "bins", "column": "units", "count": 3},
        {"op": "date_parts", "column": "date", "parts": ["year", "month_name"]},
        {"op": "split_column", "column": "date_month_name", "delimiter": "a", "into": 2},
        {"op": "merge_columns", "columns": ["region", "channel"], "name": "where", "separator": " / "},
        {"op": "sort", "by": [{"column": "revenue", "dir": "desc"}]},
        {"op": "keep_rows", "n": 10},
    )
    assert len(out.index) == 10 and set(out["channel"]) == {"Online"} and (out["units"] > 3).all()
    assert out["revenue"].is_monotonic_decreasing
    assert out["margin"].iloc[0] == pytest.approx(out["profit"].iloc[0] / out["revenue"].iloc[0])
    assert set(out["size"]) <= {"Large", "Small"} and out["units_bin"].str.contains("–").all()
    assert out["where"].iloc[0].endswith(" / Online") and "date_year" in out


def test_group_pivot_unpivot_outliers(retail):
    grouped = run(
        retail,
        {
            "op": "group_by",
            "by": ["region"],
            "aggregations": [
                {"column": "revenue", "agg": "sum", "name": "revenue"},
                {"agg": "count", "name": "orders"},
            ],
        },
    )
    assert len(grouped.index) == 5 and grouped["orders"].sum() == len(retail.index)
    assert grouped["revenue"].sum() == pytest.approx(retail["revenue"].sum())
    pivot = run(
        retail, {"op": "pivot", "index": ["region"], "columns": "channel", "values": "revenue", "agg": "sum"}
    )
    assert list(pivot.columns) == ["region", "Online", "Partner", "Retail store"]
    long = run(pivot, {"op": "unpivot", "keep": ["region"], "name": "channel", "value": "revenue"})
    assert len(long.index) == 15 and long["revenue"].sum() == pytest.approx(retail["revenue"].sum())
    capped = run(retail, {"op": "outliers", "column": "revenue", "action": "cap"})
    q1, q3 = retail["revenue"].quantile([0.25, 0.75])
    assert capped["revenue"].max() <= q3 + 1.5 * (q3 - q1) + 1e-9
    flagged = run(retail, {"op": "outliers", "column": "revenue", "action": "flag"})
    assert flagged["revenue_outlier"].dtype == bool


def test_merge_append_and_date_table(retail):
    targets = pd.DataFrame({"region": ["East", "West"], "goal": [100, 200]})
    merged = run(
        retail,
        {"op": "merge", "dataset": "t", "left_on": "region", "right_on": "region", "how": "inner"},
        others={"t": targets},
    )
    assert set(merged["region"]) == {"East", "West"} and "goal" in merged
    appended = run(retail.head(3), {"op": "append", "dataset": "t"}, others={"t": retail.head(2)})
    assert len(appended.index) == 5
    calendar = run(retail, {"op": "date_table", "column": "date", "fiscal_start": 7})
    assert calendar["date"].min() == retail["date"].min().normalize() and len(calendar.index) == 731
    july = calendar[calendar["date"] == "2024-07-01"].iloc[0]
    assert july["fiscal_year"] == 2025 and july["quarter"] == 3


def test_preview_stops_and_disabled_steps_are_skipped(retail):
    steps = [
        {"op": "keep_rows", "n": 100},
        {"op": "keep_rows", "n": 5, "enabled": False},
        {"op": "keep_rows", "n": 10},
    ]
    assert len(run(retail, *steps, upto=0).index) == 100
    assert len(run(retail, *steps).index) == 10


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([{"op": "drop_table"}], "unknown step"),
        ([{"op": "rename", "column": "a"}], "set name"),
        (
            [{"op": "calculated_column", "name": "x", "expression": "__import__('os')"}],
            "Unexpected character",
        ),
        ([{"op": "filter_rows", "conditions": [{"field": "a", "op": "regex"}]}], "unknown condition"),
        ([{"op": "keep_rows", "n": 1}] * 61, "At most 60"),
        ([{"op": "append", "dataset": str(i)} for i in range(4)], "at most 3"),
    ],
)
def test_bad_pipelines_are_rejected(steps, message):
    with pytest.raises(viz_steps.StepError, match=message):
        viz_steps.validate(steps)


def test_steps_fail_with_a_named_message(retail):
    with pytest.raises(viz_steps.StepError, match="Step 1.*no column 'nope'"):
        run(retail, {"op": "remove_columns", "columns": ["nope"]})
    with pytest.raises(viz_steps.StepError, match="SUM"):
        run(retail, {"op": "calculated_column", "name": "x", "expression": "SUM([revenue])"})
    with pytest.raises(viz_steps.StepError, match="already exists"):
        run(retail, {"op": "calculated_column", "name": "revenue", "expression": "[units] * 2"})


@pytest.mark.django_db
def test_applied_steps_through_the_api():
    org, owner = _org("steps-api")
    api = _client(owner, org)
    dataset = _sample(api)
    other = _sample(api)
    pipeline = [
        {"op": "filter_rows", "conditions": [{"field": "region", "op": "in", "value": ["East"]}]},
        {"op": "append", "dataset": other["id"]},
        {"op": "group_by", "by": ["region"], "aggregations": [{"agg": "count", "name": "rows"}]},
    ]
    preview = api.post(
        f"{BASE}/datasets/{dataset['id']}/organize/",
        {"steps": {"pipeline": pipeline, "upto": 0}},
        format="json",
    )
    assert preview.status_code == 200, preview.content
    assert {row["region"] for row in preview.json()["preview"]} == {"East"}
    saved = api.post(
        f"{BASE}/datasets/{dataset['id']}/organize/",
        {"steps": {"pipeline": pipeline}, "saveAs": "Regions"},
        format="json",
    )
    assert saved.status_code == 201, saved.content
    assert saved.json()["row_count"] == 5
    bad = api.post(
        f"{BASE}/datasets/{dataset['id']}/organize/", {"steps": {"pipeline": [{"op": "nope"}]}}, format="json"
    )
    assert bad.status_code == 400 and "unknown step" in bad.content.decode()
    stranger_org, stranger = _org("steps-stranger")
    foreign = _sample(_client(stranger, stranger_org))
    blocked = api.post(
        f"{BASE}/datasets/{dataset['id']}/organize/",
        {"steps": {"pipeline": [{"op": "append", "dataset": foreign["id"]}]}},
        format="json",
    )
    assert blocked.status_code == 404, "another organization's data cannot be merged in"
