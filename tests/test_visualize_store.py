"""The Postgres query store answers dashboard queries exactly like the pandas path."""

from __future__ import annotations

import datetime as dt
import math

import pytest
from django.db import connection

from apps.visualize import samples, services, store, visual_plan, viz_engine
from apps.visualize.models import DatasetTable
from tests.test_visualize_api import _org
from tests.test_visualize_engine import LIMITS

TODAY = dt.date(2025, 6, 15)
TRICKY = (
    b"id,region,amount,when,note\n"
    b"1,East,10.5,2024-01-03,Alpha\n"
    b"2,West,,2024-01-20,beta 50%\n"
    b"3,,7,2024-02-11,\n"
    b"4,East,3,,Gamma_x\n"
    b"5,west,12,2024-03-05,alpha\n"
    b"6,East,4.25,2024-03-31 18:30:00,alpha\n"
)


def _dataset(csv: bytes, label: str):
    org, owner = _org(label)
    profile = services.engine("viz_preview", content=csv, mime_type="text/csv", rows=1)["profile"]
    return services.create_dataset(
        name=label, csv=csv, profile=profile, owner=owner, organization=org, provenance={"kind": "test"}
    )


def _pandas(csv: bytes):
    frame = viz_engine.d.base.dataframe_from_bytes(csv, mime_type="text/csv", limits=LIMITS)
    kinds = viz_engine.column_kinds(frame)[1]
    return viz_engine.d.typed(frame, kinds), kinds


def _normal(rows):
    def cell(value):
        if isinstance(value, float):
            return None if math.isnan(value) else round(value, 6)
        return value

    return sorted(([cell(v) for v in row] for row in rows), key=repr)


TRICKY_QUERIES = [
    {
        "groupBy": [{"field": "region"}],
        "measures": [
            {"field": "*"},
            {"field": "amount", "agg": "sum"},
            {"field": "amount", "agg": "avg"},
            {"field": "amount", "agg": "count"},
            {"field": "note", "agg": "distinct"},
            {"field": "when", "agg": "min"},
            {"field": "when", "agg": "max"},
            {"field": "amount", "agg": "median"},
            {"field": "amount", "agg": "std"},
            {"field": "note", "agg": "first"},
            {"field": "note", "agg": "last"},
        ],
    },
    *[
        {"groupBy": [{"field": "when", "grain": g}], "measures": [{"field": "amount"}]}
        for g in visual_plan.GRAINS
    ],
    {"measures": [{"field": "amount"}, {"field": "*"}]},
    {"measures": [{"field": "*"}], "filters": [{"field": "region", "op": "in", "value": ["East", None]}]},
    {"measures": [{"field": "*"}], "filters": [{"field": "region", "op": "ne", "value": "East"}]},
    {"measures": [{"field": "*"}], "filters": [{"field": "region", "op": "not_in", "value": ["East"]}]},
    {"measures": [{"field": "*"}], "filters": [{"field": "note", "op": "contains", "value": "50%"}]},
    {"measures": [{"field": "*"}], "filters": [{"field": "note", "op": "contains", "value": "_x"}]},
    {"measures": [{"field": "*"}], "filters": [{"field": "note", "op": "starts_with", "value": "AL"}]},
    {"measures": [{"field": "*"}], "filters": [{"field": "amount", "op": "between", "value": [3, 10.5]}]},
    {"measures": [{"field": "*"}], "filters": [{"field": "amount", "op": "blank"}]},
    {"measures": [{"field": "*"}], "filters": [{"field": "region", "op": "not_blank"}]},
    {"measures": [{"field": "*"}], "filters": [{"field": "when", "op": "gte", "value": "2024-01-15"}]},
    {
        "measures": [{"field": "*"}],
        "filters": [{"field": "when", "op": "between", "value": ["2024-03-01", "2024-03-31T23:59:59"]}],
    },
]


@pytest.mark.django_db
@pytest.mark.parametrize("index", range(len(TRICKY_QUERIES)))
def test_sql_and_pandas_agree_on_tricky_data(index):
    dataset = _dataset(TRICKY, f"tricky{index}")
    record = store.ensure_table(dataset)
    frame, kinds = _pandas(TRICKY)
    query = visual_plan.clean_query(TRICKY_QUERIES[index], kinds, TODAY)
    via_sql = store.run_query(record, query)
    via_pandas = viz_engine.vq.aggregate(frame, query, kinds)
    assert via_sql["columns"] == via_pandas["columns"]
    assert _normal(via_sql["rows"]) == _normal(via_pandas["rows"]), (via_sql["rows"], via_pandas["rows"])


@pytest.mark.django_db
def test_sql_and_pandas_agree_on_the_retail_sample():
    csv = samples.sample_csv("retail_sales").encode()
    dataset = _dataset(csv, "retail-store")
    record = store.ensure_table(dataset)
    assert record.row_count == 2400 and record.status == DatasetTable.Status.READY
    frame, kinds = _pandas(csv)
    for raw in (
        {
            "groupBy": [{"field": "region"}, {"field": "channel"}],
            "measures": [{"field": "revenue"}, {"field": "profit", "agg": "avg"}],
        },
        {
            "groupBy": [{"field": "date", "grain": "month"}],
            "measures": [{"field": "revenue"}, {"field": "units"}],
        },
        {
            "groupBy": [{"field": "product"}],
            "measures": [{"field": "satisfaction", "agg": "median"}],
            "filters": [{"field": "channel", "op": "in", "value": ["Online"]}],
        },
    ):
        query = visual_plan.clean_query(raw, kinds, TODAY)
        assert _normal(store.run_query(record, query)["rows"]) == _normal(
            viz_engine.vq.aggregate(frame, query, kinds)["rows"]
        )


@pytest.mark.django_db
def test_the_table_is_rebuilt_when_the_data_changes_and_queries_run_read_only():
    dataset = _dataset(TRICKY, "rebuild")
    first = store.ensure_table(dataset)
    assert store.ensure_table(dataset).built_at == first.built_at, "a fresh table is reused"
    dataset.source_checksum_sha256 = "changed"
    dataset.save(update_fields=["source_checksum_sha256"])
    assert store.ready_table(dataset) is None
    rebuilt = store.ensure_table(dataset)
    assert rebuilt.built_at > first.built_at
    query = visual_plan.clean_query({"measures": [{"field": "*"}]}, {"id": "numeric"}, TODAY)
    assert store.run_query(rebuilt, query)["rows"] == [[6]]
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user, current_setting('statement_timeout')")
        user, timeout = cursor.fetchone()
    assert user != store.READER, "the read-only role does not leak into later queries"
    assert timeout != "15s"
    if store._reader():
        with connection.cursor() as cursor:
            cursor.execute("SELECT has_table_privilege(%s, 'public.identity_user', 'SELECT')", [store.READER])
            assert cursor.fetchone()[0] is False, "the reader role cannot see application tables"


def test_unknown_fields_and_unsafe_names_are_rejected():
    kinds = {"region": "categorical", "amount": "numeric"}
    with pytest.raises(visual_plan.PlanError):
        visual_plan.clean_query({"groupBy": [{"field": "region; DROP TABLE x"}]}, kinds, TODAY)
    with pytest.raises(visual_plan.PlanError):
        visual_plan.clean_query({"measures": [{"field": "amount", "agg": "pg_sleep"}]}, kinds, TODAY)
    with pytest.raises(visual_plan.PlanError):
        visual_plan.clean_query({"groupBy": [{"field": "region", "grain": "month"}]}, kinds, TODAY)
    summed_text = visual_plan.clean_query({"measures": [{"field": "region", "agg": "sum"}]}, kinds, TODAY)
    assert summed_text["measures"][0]["agg"] == "count"


def test_relative_dates():
    assert visual_plan.relative_range({"unit": "month", "mode": "last", "n": 3}, TODAY) == (
        "2025-03-01",
        "2025-05-31T23:59:59",
    )
    assert visual_plan.relative_range({"unit": "year", "mode": "this"}, TODAY) == (
        "2025-01-01",
        "2025-12-31T23:59:59",
    )
    assert visual_plan.relative_range({"unit": "day", "mode": "last", "n": 7, "complete": False}, TODAY) == (
        "2025-06-08",
        "2025-06-15T23:59:59",
    )
    assert visual_plan.relative_range({"unit": "quarter", "mode": "next"}, TODAY) == (
        "2025-07-01",
        "2025-09-30T23:59:59",
    )


@pytest.mark.django_db
def test_raw_rows_are_sampled_identically():
    csv = samples.sample_csv("retail_sales").encode()
    dataset = _dataset(csv, "rows-store")
    record = store.ensure_table(dataset)
    frame, kinds = _pandas(csv)
    filters = [visual_plan.clean_filter({"field": "channel", "op": "in", "value": ["Online"]}, kinds, TODAY)]
    for query in (
        visual_plan.clean_rows(["revenue", "date"], [], 300),
        visual_plan.clean_rows(["revenue", "channel"], filters, 250),
        visual_plan.clean_rows(["units"], filters, 5000),
    ):
        via_sql = store.run_query(record, query)
        via_pandas = viz_engine.vq.aggregate(frame, query, kinds)
        assert via_sql["columns"] == via_pandas["columns"] == query["columns"]
        assert via_sql["rows"] == via_pandas["rows"], query
        assert len(via_sql["rows"]) <= query["limit"]
