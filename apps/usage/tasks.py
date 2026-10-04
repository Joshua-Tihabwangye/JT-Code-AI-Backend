"""Metering background jobs: settlement sweep and provider usage reconciliation."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from celery import shared_task
from django.conf import settings
from django.db.models import Count, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

_INLINE_SOURCES = {"image_generation", "document_render", "knowledge_search", "embedding"}


@shared_task
def settle_finished_reservations(batch_size: int = 500) -> dict[str, int]:
    """Settle or release every hold whose source has finished; expire leaked holds."""
    from apps.usage.models import UsageReservation
    from apps.usage.services import finalize_source, release

    now = timezone.now()
    counts = {"settled": 0, "released": 0, "expired": 0, "pending": 0}
    held = UsageReservation.objects.filter(
        status=UsageReservation.Status.HELD, created_at__lt=now - timedelta(seconds=30)
    ).order_by("created_at")[:batch_size]
    for reservation in held:
        if reservation.source_type in _INLINE_SOURCES and reservation.expires_at > now:
            continue  # synchronous requests settle themselves; wait for the TTL
        outcome = finalize_source(reservation.source_type, reservation.source_id)
        if outcome in {"settled", "released"}:
            counts[outcome] += 1
        elif outcome == "unknown" or (
            outcome == "pending" and reservation.expires_at <= now - timedelta(days=1)
        ):
            release(reservation.id, reason="reservation expired without a resolvable source", expired=True)
            counts["expired"] += 1
        else:
            counts["pending"] += 1
    return counts


def _recomputed_cost(run: Any) -> Decimal:
    model = run.model
    return (
        Decimal(run.input_tokens) * model.input_price_per_token
        + Decimal(run.output_tokens) * model.output_price_per_token
        + Decimal(run.cached_tokens) * model.cached_input_price_per_token
    )


def reconcile_day(day: date) -> list[Any]:
    """Compare a day's model-run costs with billed usage, per organization and provider."""
    from apps.ai_gateway.models import ModelRun
    from apps.usage.models import UsageReconciliation, UsageRecord, UsageReservation

    start = timezone.make_aware(datetime.combine(day, datetime.min.time()))
    end = start + timedelta(days=1)
    runs = ModelRun.objects.filter(
        status=ModelRun.Status.COMPLETED, created_at__gte=start, created_at__lt=end
    ).select_related("provider", "model")
    groups: dict[tuple[Any, str], list[Any]] = {}
    for run in runs.iterator(chunk_size=1000):
        groups.setdefault((run.organization_id, run.provider.type or run.provider.name), []).append(run)

    billed_sources = {
        (record.source_type, record.source_id)
        for record in UsageRecord.objects.filter(created_at__gte=start - timedelta(days=1)).only(
            "source_type", "source_id"
        )
    }
    held_sources = {
        (reservation.source_type, reservation.source_id)
        for reservation in UsageReservation.objects.filter(status=UsageReservation.Status.HELD).only(
            "source_type", "source_id"
        )
    }
    known = billed_sources | held_sources
    from apps.agents.models import AgentRun

    agent_ids = [source_id for source_type, source_id in known if source_type == "agent_run"]
    known_agent_requests = {
        str(value) for value in AgentRun.objects.filter(id__in=agent_ids).values_list("request_id", flat=True)
    }
    drift_ratio = Decimal(str(settings.USAGE_RECONCILIATION_DRIFT_RATIO))
    results = []
    for (organization_id, provider), provider_runs in groups.items():
        recorded = sum((Decimal(run.provider_cost_usd) for run in provider_runs), Decimal("0"))
        recomputed = sum((_recomputed_cost(run) for run in provider_runs), Decimal("0"))
        unbilled = [
            run
            for run in provider_runs
            if ("job", str(run.job_id)) not in known
            and ("chat_request", str(run.request_id)) not in known
            and str(run.request_id) not in known_agent_requests
        ]
        unbilled_cost = sum((Decimal(run.provider_cost_usd) for run in unbilled), Decimal("0"))
        billed = recorded - unbilled_cost
        drift = abs(recorded - recomputed) > max(Decimal("0.000001"), recorded * drift_ratio)
        status = (
            UsageReconciliation.Status.DRIFT if drift or unbilled_cost > 0 else UsageReconciliation.Status.OK
        )
        reconciliation, _ = UsageReconciliation.objects.update_or_create(
            date=day,
            organization_id=organization_id,
            provider=provider,
            defaults={
                "model_runs": len(provider_runs),
                "model_run_cost_usd": recorded,
                "recomputed_cost_usd": recomputed,
                "billed_cost_usd": billed,
                "unbilled_runs": len(unbilled),
                "unbilled_cost_usd": unbilled_cost,
                "status": status,
                "details": {
                    "priceDrift": bool(drift),
                    "unbilledRunIds": [str(run.id) for run in unbilled[:50]],
                },
            },
        )
        results.append(reconciliation)
    return results


@shared_task
def reconcile_provider_usage(day: str | None = None) -> dict[str, int]:
    """Reconcile yesterday (or ``day``) and report drifts."""
    target = date.fromisoformat(day) if day else (timezone.now() - timedelta(days=1)).date()
    results = reconcile_day(target)
    drifts = [result for result in results if result.status == result.Status.DRIFT]
    for result in drifts:
        logger.warning(
            "Usage reconciliation drift",
            extra={
                "date": str(result.date),
                "provider": result.provider,
                "organization_id": str(result.organization_id),
                "unbilled_cost_usd": str(result.unbilled_cost_usd),
            },
        )
    return {"groups": len(results), "drifts": len(drifts)}


def usage_totals(queryset: Any) -> dict[str, Any]:
    totals = queryset.aggregate(
        credits=Sum("credits_charged"),
        uncollected=Sum("credits_uncollected"),
        cost=Sum("provider_cost_usd"),
        units=Sum("quantity"),
        records=Count("id"),
    )
    return {
        "credits": str(totals["credits"] or 0),
        "uncollectedCredits": str(totals["uncollected"] or 0),
        "providerCostUsd": str(totals["cost"] or 0),
        "units": int(totals["units"] or 0),
        "records": int(totals["records"] or 0),
    }
