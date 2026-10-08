"""Phase 19 exit criteria: measured capacity plan and sustained-load evidence support launch.

The evidence itself comes from staging runs (``verification.yml``); these tests
prove the measuring and planning instruments, plus the scale features they
drive: connection budget, index audit, capacity plan from evidence, cost
anomaly detection, read-only failover mode and Kafka partition sizing.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
import yaml
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.identity.models import Organization
from apps.operations.models import VerificationRun

pytestmark = pytest.mark.django_db


def test_connection_budget_is_derived_from_the_production_manifests():
    from apps.operations.capacity import connection_budget

    budget = connection_budget("production")
    by_name = {c["deployment"]: c for c in budget["components"]}
    assert by_name["jt-code-api"]["maxReplicas"] == 20  # production HPA ceiling (overlay patch)
    assert by_name["jt-code-worker-ai"]["maxReplicas"] == 20  # production ScaledObject override
    assert by_name["jt-code-worker-ai"]["connectionsPerPod"] == 9  # concurrency 8 + main process
    assert "jt-code-streamlit" not in by_name  # no database access
    assert budget["totalMaxClientConnections"] == sum(c["maxConnections"] for c in budget["components"])
    staging = connection_budget("staging")["totalMaxClientConnections"]
    assert staging < budget["totalMaxClientConnections"]


def test_capacity_audit_inspects_the_live_database_and_records_evidence():
    out = StringIO()
    call_command("capacity_audit", "--format", "json", stdout=out)
    report = json.loads(out.getvalue())
    database = report["database"]
    assert database["maxConnections"] > 0 and database["connectionsInUse"] >= 1
    assert database["unindexedForeignKeysOnLargeTables"] == []
    assert isinstance(database["partitionCandidates"], list)
    assert VerificationRun.objects.get(kind="capacity_audit").status == VerificationRun.Status.PASSED
    with pytest.raises(CommandError, match="exceed the pooler limit"):
        call_command("capacity_audit", "--pooler-max-clients", "10", stdout=StringIO())


def test_capacity_plan_uses_measured_evidence():
    with pytest.raises(CommandError, match="No measured throughput"):
        call_command("capacity_plan", stdout=StringIO())
    VerificationRun.objects.create(
        kind=VerificationRun.Kind.LOAD_TEST,
        status=VerificationRun.Status.PASSED,
        summary={"summary": {"throughputRps": 300.0}, "parameters": {"apiPods": 3}},
    )
    VerificationRun.objects.create(
        kind=VerificationRun.Kind.SATURATION_DRILL,
        status=VerificationRun.Status.PASSED,
        summary={
            "celery": {
                "queues": {
                    q: {"throughputPerSecond": 2.0}
                    for q in (
                        "jobs.analysis",
                        "jobs.ingestion",
                        "jobs.visualization",
                        "analytics.analysis",
                        "jobs.default",
                    )
                }
            },
            "kafka": {"consumeThroughputPerSecond": 400.0},
        },
    )
    out = StringIO()
    call_command("capacity_plan", "--users", "100000", stdout=out)
    result = json.loads(out.getvalue())
    # 100k users * 20% DAU * 10% peak = 2,000 active; 12 req/min each = 400 req/s;
    # 100 req/s per pod with 1.5x headroom -> 6 pods.
    assert result["peakActiveUsers"] == 2000 and result["peakRequestsPerSecond"] == 400.0
    assert result["apiPods"] == 6
    assert result["workers"]["jobs.analysis"]["pods"] == 3  # 3.33 jobs/s * 1.5 / 2 per pod
    assert result["kafkaPartitions"] == 3
    assert result["gaps"] == []
    huge = json.loads(
        (lambda o: (call_command("capacity_plan", "--users", "2000000", stdout=o), o.getvalue())[1])(
            StringIO()
        )
    )
    assert huge["gaps"] and "HPA allows" in huge["gaps"][0]


def _usage(org, user, *, cost: str, at):
    from apps.usage.models import UsageRecord

    record = UsageRecord.objects.create(
        organization=org,
        user=user,
        feature="chat_messages",
        source_type="test",
        source_id=f"{org.id.hex[:8]}-{int(at.timestamp())}-{cost}",
        basis="provider_cost",
        credits_charged=Decimal("1"),
        provider_cost_usd=Decimal(cost),
        period=at.strftime("%Y-%m"),
    )
    from django.db import connection

    with connection.cursor() as cursor:  # created_at is auto_now_add; the ledger is append-only
        cursor.execute("SELECT set_config('jt_code.ledger_purge', 'on', true)")
    UsageRecord.objects.filter(id=record.id).update(created_at=at)
    return record


def test_cost_anomalies_are_detected_once_and_published(user, settings):
    from apps.usage.anomalies import detect_cost_anomalies

    from apps.events.models import OutboxEvent
    from apps.usage.models import CostAnomaly

    settings.USAGE_ANOMALY_Z = 4.0
    settings.USAGE_ANOMALY_MIN_USD = 5.0
    org = Organization.objects.create(name="Spender", owner=user)
    now = timezone.now().replace(minute=30)
    hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    for days_back in range(1, 8):
        _usage(org, user, cost="0.50", at=hour - timedelta(days=days_back))
    _usage(org, user, cost="40.00", at=hour + timedelta(minutes=10))  # the spike

    steady = Organization.objects.create(name="Steady", owner=user)
    settings.USAGE_ANOMALY_BASELINE_DAYS = 1
    for hours_back in range(1, 25):
        _usage(steady, user, cost="6.00", at=hour - timedelta(hours=hours_back) + timedelta(minutes=5))
    _usage(steady, user, cost="6.00", at=hour + timedelta(minutes=5))  # same as every other hour

    found = detect_cost_anomalies(now)
    flagged = {a.organization_id for a in found}
    assert org.id in flagged
    assert steady.id not in flagged
    anomaly = CostAnomaly.objects.get(organization=org)
    assert anomaly.observed_usd == Decimal("40.00000000") and anomaly.zscore >= 4
    assert OutboxEvent.objects.filter(
        topic__endswith="usage.cost.anomaly", payload__anomaly_id=str(anomaly.id)
    ).exists()
    assert detect_cost_anomalies(now) == []  # idempotent per scope and hour


def test_read_only_mode_rejects_writes_but_serves_reads(authenticated_client, settings):
    settings.READ_ONLY_MODE = True
    assert authenticated_client.get("/api/v1/me/").status_code == 200
    blocked = authenticated_client.post("/api/v1/conversations/", {"title": "x"}, format="json")
    assert blocked.status_code == 503 and blocked["Retry-After"] == "300"
    assert blocked.json()["code"] == "read_only"
    assert authenticated_client.get("/api/v1/health/live/").status_code == 200


def test_kafka_partitions_follow_capacity_overrides(settings):
    from apps.events.management.commands.ensure_kafka_topics import partitions_for

    settings.KAFKA_TOPIC_PARTITIONS = 3
    settings.KAFKA_TOPIC_PARTITIONS_OVERRIDES = {"chat.request.accepted": 24}
    assert partitions_for(f"{settings.KAFKA_TOPIC_PREFIX}.chat.request.accepted") == 24
    assert partitions_for(f"{settings.KAFKA_TOPIC_PREFIX}.jobs.job.created") == 3


def test_slo_burn_rate_and_cost_alerts_exist():
    alerts = yaml.safe_load((settings.BASE_DIR / "infra/prometheus/alerts.yml").read_text())
    names = {rule["alert"] for group in alerts["groups"] for rule in group["rules"]}
    assert {"SLOErrorBudgetFastBurn", "SLOErrorBudgetSlowBurn", "CostAnomalyDetected"} <= names


def test_scale_documents_exist_and_cover_the_tier_review():
    docs = settings.BASE_DIR / "docs"
    assert "99.95%" in (docs / "SLOs.md").read_text()
    assert "Measured findings" in (docs / "CAPACITY_PLAN.md").read_text()
    assert "Region" in (docs / "DEPENDENCY_FAILURE_PLAN.md").read_text()
    assert "Measure first" in (docs / "adr" / "ADR-006-table-partitioning.md").read_text()
