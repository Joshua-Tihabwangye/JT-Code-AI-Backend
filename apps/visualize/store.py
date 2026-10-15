"""The Postgres (Supabase) query store for dashboards.

Every dataset used by a dashboard gets a typed copy in the private ``viz_data`` schema, loaded with
``COPY`` from clean CSV produced by the sandboxed engine. Dashboard queries (``visual_plan``) are
compiled here to SQL so the database does the filtering and aggregation — millions of rows never
leave Postgres; only the small grouped result goes to the Python engine that draws the visual.

Safety:
* identifiers come only from the stored ``c0…cN`` mapping (``psycopg.sql.Identifier``); every value
  is a bound parameter with an explicit cast;
* queries run as ``jt_viz_reader`` (``SET LOCAL ROLE``; it can only read ``viz_data``) under a
  statement timeout, inside a savepoint so the role and timeout never leak;
* the stored file stays the source of truth: the table is rebuilt whenever its checksum changes.
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import logging
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone
from psycopg import sql

from apps.analytics.models import Dataset
from apps.visualize.models import DatasetTable

logger = logging.getLogger(__name__)

SCHEMA = "viz_data"
READER = "jt_viz_reader"
SQL_TYPES = {"numeric": "double precision", "datetime": "timestamp", "boolean": "boolean"}
CASTS = {"numeric": "float8", "datetime": "timestamp", "boolean": "boolean"}
MAX_INDEXES = 6
_reader_available: bool | None = None


class StoreError(RuntimeError):
    """The dataset's Postgres table could not be built or queried."""


def dataset_token(dataset: Dataset) -> str:
    """Identifies the dataset's current content (changes on replace, edit and organize)."""
    return dataset.source_checksum_sha256 or dataset.updated_at.isoformat()


def table_name(dataset: Dataset) -> str:
    return f"ds_{dataset.id.hex}"


def _ident(*names: str) -> sql.Identifier:
    return sql.Identifier(*names)


def _text(statement: sql.Composable) -> str:
    connection.ensure_connection()
    return statement.as_string(connection.connection)


def ready_table(dataset: Dataset) -> DatasetTable | None:
    record = DatasetTable.objects.filter(dataset=dataset).first()
    if (
        record
        and record.status == DatasetTable.Status.READY
        and record.source_checksum == dataset_token(dataset)
    ):
        return record
    return None


def ensure_table(dataset: Dataset) -> DatasetTable:
    """The dataset's ready table, (re)building it when missing or stale."""
    return ready_table(dataset) or build_table(dataset)


def build_table(dataset: Dataset) -> DatasetTable:
    from apps.visualize import services

    token = dataset_token(dataset)
    output = services.engine("viz_table", dataset)
    content = base64.b64decode(output["csv"])
    columns = list(output["columns"])
    name = table_name(dataset)
    profiles = {entry["name"]: entry for entry in (dataset.profile or {}).get("columnProfiles", [])}
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [name])
            record, _ = DatasetTable.objects.select_for_update().get_or_create(
                dataset=dataset, defaults={"table_name": name}
            )
            if record.status == DatasetTable.Status.READY and record.source_checksum == token:
                return record
            target = _ident(SCHEMA, name)
            cursor.execute(_text(sql.SQL("DROP TABLE IF EXISTS {}").format(target)))
            definitions = [
                sql.SQL("{} {}").format(_ident(c["column"]), sql.SQL(SQL_TYPES.get(c["kind"], "text")))
                for c in columns
            ]
            definitions.append(sql.SQL("{} bigint").format(_ident("_row")))
            cursor.execute(
                _text(sql.SQL("CREATE TABLE {} ({})").format(target, sql.SQL(", ").join(definitions)))
            )
            names = sql.SQL(", ").join([_ident(c["column"]) for c in columns] + [_ident("_row")])
            copy = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER true)").format(target, names)
            with cursor.cursor.copy(_text(copy)) as writer:
                for start in range(0, len(content), 1 << 20):
                    writer.write(content[start : start + (1 << 20)])
            indexed = 0
            for column in columns:
                distinct = profiles.get(column["name"], {}).get("distinct", 0)
                if indexed < MAX_INDEXES and (
                    column["kind"] == "datetime"
                    or (column["kind"] in {"categorical", "boolean"} and distinct <= 500)
                ):
                    cursor.execute(
                        _text(
                            sql.SQL("CREATE INDEX {} ON {} ({})").format(
                                _ident(f"{name}_{column['column']}_idx"), target, _ident(column["column"])
                            )
                        )
                    )
                    indexed += 1
            cursor.execute(_text(sql.SQL("ANALYZE {}").format(target)))
            if _reader():
                cursor.execute(_text(sql.SQL("GRANT SELECT ON {} TO {}").format(target, _ident(READER))))
        record.table_name = name
        record.columns = columns
        record.row_count = int(output["rows"])
        record.source_checksum = token
        record.status = DatasetTable.Status.READY
        record.error = ""
        record.built_at = timezone.now()
        record.save()
    return record


def _reader() -> bool:
    """Whether the application role may switch to the read-only reader role."""
    global _reader_available
    if _reader_available is None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s) "
                "AND pg_has_role(current_user, %s, 'MEMBER')",
                [READER, READER],
            )
            _reader_available = bool(cursor.fetchone()[0])
    return _reader_available


@receiver(post_delete, sender=DatasetTable)
def _drop_table(sender: type[DatasetTable], instance: DatasetTable, **_: Any) -> None:
    def drop() -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                _text(sql.SQL("DROP TABLE IF EXISTS {}").format(_ident(SCHEMA, instance.table_name)))
            )

    transaction.on_commit(drop)


# --- Query compilation ---------------------------------------------------------------------------


def _column(record: DatasetTable, field: str) -> dict[str, Any]:
    for column in record.columns:
        if column["name"] == field:
            return column
    raise StoreError(f"{field} is not in the dataset table.")


def _cast(kind: str) -> sql.SQL:
    return sql.SQL(CASTS.get(kind, "text"))


def _measure(record: DatasetTable, measure: dict[str, Any]) -> sql.Composable:
    if measure["field"] == "*":
        return sql.SQL("COUNT(*)")
    column = _ident(_column(record, measure["field"])["column"])
    agg = measure["agg"]
    simple = {"sum": "SUM", "avg": "AVG", "count": "COUNT", "min": "MIN", "max": "MAX", "std": "STDDEV_SAMP"}
    if agg in simple:
        return sql.SQL("{}({})").format(sql.SQL(simple[agg]), column)
    if agg == "distinct":
        return sql.SQL("COUNT(DISTINCT {})").format(column)
    if agg == "median":
        return sql.SQL("percentile_cont(0.5) WITHIN GROUP (ORDER BY {})").format(column)
    direction = sql.SQL("DESC" if agg == "last" else "ASC")
    return sql.SQL("(array_agg({c} ORDER BY {row} {d}) FILTER (WHERE {c} IS NOT NULL))[1]").format(
        c=column, row=_ident("_row"), d=direction
    )


def _like(value: str, op: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return {"contains": f"%{escaped}%", "starts_with": f"{escaped}%", "ends_with": f"%{escaped}"}[op]


def _condition(record: DatasetTable, item: dict[str, Any], params: list[Any]) -> sql.Composable:
    """One filter as SQL (parameters appended in order)."""
    op, value = item["op"], item.get("value")
    if op in {"or", "and"}:
        joiner = sql.SQL(" OR " if op == "or" else " AND ")
        return sql.SQL("({})").format(joiner.join(_condition(record, part, params) for part in value))
    info = _column(record, item["field"])
    column = _ident(info["column"])
    cast = _cast(info["kind"])
    if op == "blank":
        return sql.SQL("{} IS NULL").format(column)
    if op == "not_blank":
        return sql.SQL("{} IS NOT NULL").format(column)
    if op == "top":
        # Top/bottom N members by a measure over the whole dataset; ties broken by name.
        direction = sql.SQL("ASC" if value["direction"] == "bottom" else "DESC")
        measure = _measure(record, {"field": value["by"]["field"], "agg": value["by"]["agg"]})
        return sql.SQL(
            "{c} IN (SELECT {c} FROM {t} WHERE {c} IS NOT NULL GROUP BY {c} "
            "ORDER BY {m} {d} NULLS LAST, {c} ASC LIMIT {n})"
        ).format(
            c=column,
            t=_ident(SCHEMA, record.table_name),
            m=measure,
            d=direction,
            n=sql.Literal(int(value["n"])),
        )
    if op in {"in", "not_in"}:
        values = [v for v in value if v is not None]
        hit = sql.SQL("({c} = ANY(%s::{t}[]){nulls})").format(
            c=column,
            t=cast,
            nulls=sql.SQL(" OR {} IS NULL").format(column) if None in value else sql.SQL(""),
        )
        params.append(values)
        return hit if op == "in" else sql.SQL("({} IS NOT NULL AND NOT {})").format(column, hit)
    if op == "between":
        params.extend(value)
        return sql.SQL("{c} BETWEEN %s::{t} AND %s::{t}").format(c=column, t=cast)
    if op in {"contains", "starts_with", "ends_with"}:
        params.append(_like(str(value), op))
        return sql.SQL("{}::text ILIKE %s ESCAPE '\\'").format(column)
    symbol = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
    params.append(value)
    return sql.SQL("{c} {s} %s::{t}").format(c=column, s=sql.SQL(symbol), t=cast)


def _where(record: DatasetTable, filters: list[dict[str, Any]]) -> tuple[sql.Composable, list[Any]]:
    params: list[Any] = []
    parts = [_condition(record, item, params) for item in filters]
    if not parts:
        return sql.SQL(""), params
    return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(parts), params


def compile_rows(record: DatasetTable, query: dict[str, Any]) -> tuple[str, list[Any]]:
    """Raw rows for gallery charts: every k-th row (``mod(_row, k) = 0``) when the table is large."""
    selects = [_ident(_column(record, name)["column"]) for name in query["columns"]]
    where, params = _where(record, query["filters"])
    step = max(1, -(-int(record.row_count) // query["limit"]))
    # mod() rather than %: a bare % would be read as a parameter placeholder.
    sample = sql.SQL("mod({}, {}) = 0").format(_ident("_row"), sql.Literal(step)) if step > 1 else None
    if sample is not None:
        # Each filter adds one condition, so the WHERE clause is empty exactly when there are none.
        where = where + sql.SQL(" AND ") + sample if query["filters"] else sql.SQL(" WHERE ") + sample
    statement = sql.SQL("SELECT {} FROM {}{} ORDER BY {} LIMIT {}").format(
        sql.SQL(", ").join(selects),
        _ident(SCHEMA, record.table_name),
        where,
        _ident("_row"),
        sql.Literal(query["limit"] + 1),
    )
    return _text(statement), params


def compile_query(record: DatasetTable, query: dict[str, Any]) -> tuple[str, list[Any]]:
    selects: list[sql.Composable] = []
    for index, group in enumerate(query["groupBy"]):
        column = _ident(_column(record, group["field"])["column"])
        expression = (
            sql.SQL("date_trunc({}, {})").format(sql.Literal(group["grain"]), column)
            if group.get("grain")
            else column
        )
        selects.append(sql.SQL("{} AS {}").format(expression, _ident(f"g{index}")))
    for measure in query["measures"]:
        selects.append(sql.SQL("{} AS {}").format(_measure(record, measure), _ident(measure["id"])))
    where, params = _where(record, query["filters"])
    statement = sql.SQL("SELECT {} FROM {}{}").format(
        sql.SQL(", ").join(selects) or sql.SQL("COUNT(*)"), _ident(SCHEMA, record.table_name), where
    )
    if query["groupBy"]:
        positions = sql.SQL(", ").join(sql.Literal(i + 1) for i in range(len(query["groupBy"])))
        ordering = sql.SQL(", ").join(
            sql.SQL("{} NULLS LAST").format(sql.Literal(i + 1)) for i in range(len(query["groupBy"]))
        )
        statement = sql.SQL("{} GROUP BY {} ORDER BY {} LIMIT {}").format(
            statement, positions, ordering, sql.Literal(query["limit"] + 1)
        )
    return _text(statement), params


def _plain(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def run_query(record: DatasetTable, query: dict[str, Any]) -> dict[str, Any]:
    if query.get("rows"):
        statement, params = compile_rows(record, query)
        columns = list(query["columns"])
    else:
        statement, params = compile_query(record, query)
        columns = [f"g{i}" for i in range(len(query["groupBy"]))] + [m["id"] for m in query["measures"]]
    timeout_ms = int(getattr(settings, "VISUALIZE_QUERY_TIMEOUT_MS", 15_000))
    reader = _reader()
    with transaction.atomic(), connection.cursor() as cursor:
        # Both settings are transaction-local and also undone if the savepoint rolls back.
        cursor.execute("SELECT current_setting('statement_timeout')")
        previous = cursor.fetchone()[0]
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", [str(timeout_ms)])
        if reader:
            cursor.execute(_text(sql.SQL("SET LOCAL ROLE {}").format(_ident(READER))))
        cursor.execute(statement, params)
        rows = [[_plain(value) for value in row] for row in cursor.fetchall()]
        if reader:
            cursor.execute("RESET ROLE")
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", [previous])
    truncated = len(rows) > query["limit"]
    return {"columns": columns, "rows": rows[: query["limit"]], "truncated": truncated}
