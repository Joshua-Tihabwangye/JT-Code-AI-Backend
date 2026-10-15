"""Deterministic (token-free) chart matching, field mapping and recommendations."""

from __future__ import annotations

import io

import pandas as pd
import pytest

from apps.visualize import catalog, recommend, samples, viz_engine


@pytest.fixture(scope="module")
def retail_profile():
    frame = pd.read_csv(io.StringIO(samples.sample_csv("retail_sales")))
    return viz_engine.column_kinds(frame)[0]


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Compare revenue by region", "bar"),
        ("Show the monthly revenue trend", "line"),
        ("share of sales by channel", "pie"),
        ("is price related to area", "scatter"),
        ("distribution of prices", "histogram"),
        ("roc curve for churn", "roc_curve"),
        ("sankey diagram of flows", "sankey"),
    ],
)
def test_prompts_match_the_expected_chart(prompt, expected):
    assert recommend.match_charts(prompt, 1) == [expected]


def test_auto_fields_fill_every_required_slot(retail_profile):
    kinds = recommend.kinds(retail_profile)
    filled = 0
    for chart in catalog.CHARTS:
        fields = recommend.auto_fields(chart.id, retail_profile)
        try:
            catalog.validate_selection(chart.slots, chart.options, fields, {}, kinds)
            filled += 1
        except ValueError:
            continue  # e.g. ML charts need prediction columns this dataset lacks
    assert filled >= 55


def test_mentioned_columns_win(retail_profile):
    fields = recommend.auto_fields("scatter", retail_profile, "is profit related to discount?")
    assert {fields["x"], fields["y"]} == {"profit", "discount"}


def test_recommendations_fit_the_data(retail_profile):
    ideas = recommend.recommendations(retail_profile)
    assert ideas[0] == {**ideas[0], "chartType": "line", "fields": {"x": "date", "y": ["revenue"]}}
    assert {idea["chartType"] for idea in ideas} >= {"bar", "scatter", "box"}
    assert len({idea["category"] for idea in ideas}) >= 5, "ideas span several chart families"
