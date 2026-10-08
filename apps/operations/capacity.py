"""Capacity measurements and planning (Phase 19).

``connection_budget`` derives the worst-case number of PostgreSQL client
connections from the Kubernetes manifests (every Deployment at its autoscaling
ceiling) - the number the Supabase pooler must accept. ``database_audit``
inspects the live database: connections in use, the largest tables, foreign
keys without an index, unused indexes, sequential-scan hotspots and tables that
crossed the partitioning thresholds. ``plan`` turns measured load-test and
saturation results into replica, connection and Kafka partition requirements
for a target user population.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import yaml
from django.conf import settings
from django.db import connection

# Tables that grow with usage; candidates for time partitioning once large.
APPEND_HEAVY_TABLES = (
    "conversations_message",
    "conversations_chatrequest",
    "ai_gateway_modelrun",
    "usage_usagerecord",
    "billing_creditledger",
    "governance_auditevent",
    "events_outboxevent",
    "events_consumedevent",
    "knowledge_chunk",
    "jobs_job",
)
PARTITION_ROWS = 50_000_000
PARTITION_BYTES = 50 * 1024**3


def _overlay_patches(overlay: Path) -> dict[str, dict[str, int]]:
    """Replica/HPA/KEDA ceilings set by JSON6902 patches in an overlay."""
    data = yaml.safe_load((overlay / "kustomization.yaml").read_text())
    ceilings: dict[str, dict[str, int]] = {}
    for patch in data.get("patches") or []:
        target = patch.get("target") or {}
        for op in yaml.safe_load(patch.get("patch") or "[]") or []:
            if isinstance(op, dict) and op.get("op") == "replace":
                key = f"{target.get('kind')}/{target.get('name', '*')}"
                ceilings.setdefault(key, {})[op["path"].rsplit("/", 1)[-1]] = int(op["value"])
    return ceilings


def connection_budget(environment: str = "production") -> dict[str, Any]:
    root = Path(settings.BASE_DIR) / "infra" / "k8s"
    patches = _overlay_patches(root / "overlays" / environment)
    keda = {
        doc["spec"]["scaleTargetRef"]["name"]: doc["spec"]["maxReplicaCount"]
        for doc in yaml.safe_load_all((root / "components" / "keda" / "scaledobjects.yaml").read_text())
        if doc and doc["kind"] == "ScaledObject"
    }
    components: list[dict[str, Any]] = []
    for path in sorted((root / "base").glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if not doc or doc.get("kind") != "Deployment":
                continue
            name = doc["metadata"]["name"]
            container = doc["spec"]["template"]["spec"]["containers"][0]
            env = {item["name"]: item.get("value") for item in container.get("env", [])}
            if name == "jt-code-streamlit":
                continue  # read-only API client; no database connections
            if name == "jt-code-api":
                hpa: dict[str, Any] = next(
                    (
                        d
                        for d in yaml.safe_load_all((root / "base" / "api.yaml").read_text())
                        if d and d["kind"] == "HorizontalPodAutoscaler"
                    ),
                    {},
                )
                replicas = patches.get("HorizontalPodAutoscaler/jt-code-api", {}).get(
                    "maxReplicas", hpa.get("spec", {}).get("maxReplicas", 1)
                )
                # Each uvicorn worker runs Django's sync code on one thread-sensitive
                # executor thread: one connection per worker process.
                per_pod = int(env.get("WEB_CONCURRENCY") or 2)
            elif "CELERY_QUEUES" in env:
                ceiling = patches.get(f"ScaledObject/{name}", {}).get("maxReplicaCount") or patches.get(
                    "ScaledObject/*", {}
                ).get("maxReplicaCount")
                replicas = ceiling or keda.get(name, doc["spec"].get("replicas", 1))
                per_pod = int(env.get("CELERY_CONCURRENCY") or 1) + 1  # pool processes + the main process
            else:
                replicas = doc["spec"].get("replicas", 1)
                per_pod = 1
            components.append(
                {
                    "deployment": name,
                    "maxReplicas": replicas,
                    "connectionsPerPod": per_pod,
                    "maxConnections": replicas * per_pod,
                }
            )
    components.append(
        {"deployment": "jt-code-migrate (job)", "maxReplicas": 1, "connectionsPerPod": 1, "maxConnections": 1}
    )
    return {
        "environment": environment,
        "components": components,
        "totalMaxClientConnections": sum(c["maxConnections"] for c in components),
    }


def _rows(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params or [])
        columns = [column.name for column in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def database_audit(*, min_fk_rows: int = 10_000) -> dict[str, Any]:
    max_connections = int(_rows("SHOW max_connections")[0]["max_connections"])
    by_state = _rows(
        "SELECT coalesce(state, 'unknown') AS state, count(*) AS count FROM pg_stat_activity "
        "WHERE datname = current_database() GROUP BY 1 ORDER BY 2 DESC"
    )
    tables = _rows(
        "SELECT relname AS table, n_live_tup AS rows, pg_total_relation_size(relid) AS bytes, "
        "seq_scan, coalesce(idx_scan, 0) AS idx_scan FROM pg_stat_user_tables "
        "WHERE schemaname = 'public' ORDER BY pg_total_relation_size(relid) DESC LIMIT 25"
    )
    unindexed = _rows(
        """
        SELECT cl.relname AS table, a.attname AS column, c.conname AS constraint, s.n_live_tup AS rows
        FROM pg_constraint c
        JOIN pg_class cl ON cl.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = cl.relnamespace AND n.nspname = 'public'
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
        LEFT JOIN pg_stat_user_tables s ON s.relid = c.conrelid
        WHERE c.contype = 'f' AND array_length(c.conkey, 1) = 1
          AND NOT EXISTS (
            SELECT 1 FROM pg_index i WHERE i.indrelid = c.conrelid AND i.indkey[0] = c.conkey[1]
          )
        ORDER BY s.n_live_tup DESC NULLS LAST, cl.relname
        """
    )
    unused = _rows(
        """
        SELECT s.relname AS table, s.indexrelname AS index, pg_relation_size(s.indexrelid) AS bytes
        FROM pg_stat_user_indexes s JOIN pg_index i ON i.indexrelid = s.indexrelid
        WHERE s.schemaname = 'public' AND s.idx_scan = 0 AND NOT i.indisunique AND NOT i.indisprimary
          AND pg_relation_size(s.indexrelid) > 1048576
        ORDER BY pg_relation_size(s.indexrelid) DESC
        """
    )
    hotspots = [
        t
        for t in tables
        if (t["rows"] or 0) > 10_000 and (t["seq_scan"] or 0) > 10 * max(1, t["idx_scan"] or 0)
    ]
    partition_candidates = [
        {**t, "reason": "rows" if (t["rows"] or 0) >= PARTITION_ROWS else "size"}
        for t in tables
        if t["table"] in APPEND_HEAVY_TABLES
        and ((t["rows"] or 0) >= PARTITION_ROWS or t["bytes"] >= PARTITION_BYTES)
    ]
    return {
        "maxConnections": max_connections,
        "connectionsByState": by_state,
        "connectionsInUse": sum(int(row["count"]) for row in by_state),
        "largestTables": tables,
        "unindexedForeignKeys": unindexed,
        "unindexedForeignKeysOnLargeTables": [row for row in unindexed if (row["rows"] or 0) >= min_fk_rows],
        "unusedIndexes": unused,
        "sequentialScanHotspots": hotspots,
        "partitionCandidates": partition_candidates,
        "partitionThresholds": {"rows": PARTITION_ROWS, "bytes": PARTITION_BYTES},
    }


# Planning ---------------------------------------------------------------------------------------


def plan(
    *,
    users: int,
    daily_active_ratio: float,
    peak_concurrency_ratio: float,
    requests_per_active_user_per_minute: float,
    measured_rps_per_api_pod: float,
    measured_jobs_per_worker_second: dict[str, float],
    jobs_per_active_user_per_hour: dict[str, float],
    events_per_request: float,
    measured_events_per_partition_second: float,
    headroom: float = 1.5,
) -> dict[str, Any]:
    """Size the platform from *measured* per-unit throughput (load tests, saturation drill)."""
    active = users * daily_active_ratio * peak_concurrency_ratio
    peak_rps = active * requests_per_active_user_per_minute / 60
    api_pods = max(2, math.ceil(peak_rps * headroom / max(measured_rps_per_api_pod, 1e-6)))
    workers = {}
    for queue, per_hour in jobs_per_active_user_per_hour.items():
        arrival = active * per_hour / 3600
        throughput = measured_jobs_per_worker_second.get(queue)
        workers[queue] = {
            "arrivalPerSecond": round(arrival, 3),
            "measuredPerWorkerPod": throughput,
            "pods": max(1, math.ceil(arrival * headroom / throughput)) if throughput else None,
        }
    events = peak_rps * events_per_request
    partitions = max(3, math.ceil(events * headroom / max(measured_events_per_partition_second, 1e-6)))
    return {
        "inputs": {
            "users": users,
            "dailyActiveRatio": daily_active_ratio,
            "peakConcurrencyRatio": peak_concurrency_ratio,
            "requestsPerActiveUserPerMinute": requests_per_active_user_per_minute,
            "headroom": headroom,
        },
        "peakActiveUsers": round(active),
        "peakRequestsPerSecond": round(peak_rps, 1),
        "apiPods": api_pods,
        "workers": workers,
        "peakEventsPerSecond": round(events, 1),
        "kafkaPartitions": partitions,
    }


def measured_inputs_from_evidence() -> dict[str, Any]:
    """Latest passing load test and saturation drill results (per-unit throughput)."""
    from apps.operations.models import VerificationRun

    load = (
        VerificationRun.objects.filter(
            kind=VerificationRun.Kind.LOAD_TEST, status=VerificationRun.Status.PASSED
        )
        .order_by("-started_at")
        .first()
    )
    saturation = (
        VerificationRun.objects.filter(
            kind=VerificationRun.Kind.SATURATION_DRILL, status=VerificationRun.Status.PASSED
        )
        .order_by("-started_at")
        .first()
    )
    measured: dict[str, Any] = {}
    if load is not None:
        summary = load.summary.get("summary", {})
        pods = int((load.summary.get("parameters") or {}).get("apiPods") or 1)
        measured["measured_rps_per_api_pod"] = float(summary.get("throughputRps") or 0) / max(pods, 1)
    if saturation is not None:
        queues = (saturation.summary.get("celery") or {}).get("queues") or {}
        measured["measured_jobs_per_worker_second"] = {
            queue: float(result.get("throughputPerSecond") or 0) for queue, result in queues.items()
        }
        kafka = saturation.summary.get("kafka") or {}
        if kafka.get("consumeThroughputPerSecond"):
            measured["measured_events_per_partition_second"] = float(kafka["consumeThroughputPerSecond"])
    return measured


def render_markdown(result: dict[str, Any]) -> str:
    lines = [f"# Capacity plan ({result['inputs']['users']:,} users)", ""]
    lines.append(f"* Peak active users: {result['peakActiveUsers']:,}")
    lines.append(f"* Peak API requests/s: {result['peakRequestsPerSecond']}")
    lines.append(f"* API pods: {result['apiPods']}")
    for queue, worker in result["workers"].items():
        lines.append(f"* `{queue}` workers: {worker['pods']} (arrival {worker['arrivalPerSecond']}/s)")
    lines.append(f"* Kafka partitions per hot topic: {result['kafkaPartitions']}")
    return "\n".join(lines) + "\n"


def dumps(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


__all__ = ["connection_budget", "database_audit", "dumps", "plan", "re"]
