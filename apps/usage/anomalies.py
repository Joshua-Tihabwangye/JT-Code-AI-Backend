"""Cost anomaly detection (Phase 19).

Every hour, the provider cost of the previous full hour is compared - per
organization and platform-wide - with the same series over the trailing
``USAGE_ANOMALY_BASELINE_DAYS`` (hours without usage count as zero). An hour is
anomalous when it is at least ``USAGE_ANOMALY_MIN_USD`` and its z-score is at
least ``USAGE_ANOMALY_Z``. Anomalies are stored once per (scope, hour), counted
in ``jt_cost_anomalies_total``, and published as ``usage.cost.anomaly`` (Kafka,
and the n8n operations notification workflow).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.db.models.functions import TruncHour
from django.utils import timezone

from apps.usage.models import CostAnomaly, UsageRecord

STDDEV_FLOOR_USD = 0.01


def _hour_floor(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def _series(start: datetime, end: datetime, *, by_org: bool) -> dict[Any, dict[datetime, float]]:
    rows = (
        UsageRecord.objects.filter(created_at__gte=start, created_at__lt=end)
        .annotate(hour=TruncHour("created_at"))
        .values(*(["organization_id", "hour"] if by_org else ["hour"]))
        .annotate(cost=Sum("provider_cost_usd"))
    )
    series: dict[Any, dict[datetime, float]] = {}
    for row in rows:
        key = row["organization_id"] if by_org else None
        series.setdefault(key, {})[row["hour"]] = float(row["cost"] or 0)
    return series


def _baseline(values: dict[datetime, float], hours: int) -> tuple[float, float]:
    total = sum(values.values())
    mean = total / hours
    variance = sum((value - mean) ** 2 for value in values.values()) + (hours - len(values)) * mean**2
    return mean, math.sqrt(variance / hours)


def detect_cost_anomalies(now: datetime | None = None) -> list[CostAnomaly]:
    from apps.core.metrics import COST_ANOMALIES
    from apps.events.outbox import enqueue_outbox_event

    now = now or timezone.now()
    hour = _hour_floor(now) - timedelta(hours=1)
    days = int(settings.USAGE_ANOMALY_BASELINE_DAYS)
    hours = days * 24
    baseline_start = hour - timedelta(hours=hours)
    found: list[CostAnomaly] = []
    for by_org in (True, False):
        current = _series(hour, hour + timedelta(hours=1), by_org=by_org)
        history = _series(baseline_start, hour, by_org=by_org)
        for scope, observed_by_hour in current.items():
            observed = sum(observed_by_hour.values())
            if observed < float(settings.USAGE_ANOMALY_MIN_USD):
                continue
            mean, stddev = _baseline(history.get(scope, {}), hours)
            zscore = (observed - mean) / max(stddev, STDDEV_FLOOR_USD)
            if zscore < float(settings.USAGE_ANOMALY_Z):
                continue
            try:
                with transaction.atomic():
                    anomaly = CostAnomaly.objects.create(
                        organization_id=scope,
                        hour=hour,
                        observed_usd=Decimal(str(round(observed, 8))),
                        expected_usd=Decimal(str(round(mean, 8))),
                        stddev_usd=Decimal(str(round(stddev, 8))),
                        zscore=round(zscore, 2),
                    )
                    enqueue_outbox_event(
                        topic="usage.cost.anomaly",
                        event_key=str(scope or "platform"),
                        payload={
                            "anomaly_id": str(anomaly.id),
                            "organization_id": str(scope) if scope else None,
                            "scope": "organization" if scope else "platform",
                            "hour": hour.isoformat(),
                            "observed_usd": str(anomaly.observed_usd),
                            "expected_usd": str(anomaly.expected_usd),
                            "zscore": anomaly.zscore,
                        },
                    )
            except IntegrityError:
                continue  # already recorded for this scope and hour
            COST_ANOMALIES.labels("organization" if scope else "platform").inc()
            found.append(anomaly)
    return found
