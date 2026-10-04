"""Usage API: tenant usage/quota view and staff-only internal dashboards."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.views import APIView
from apps.identity.authorization import organization_for_request
from apps.usage.concurrency import KINDS, active_count, concurrency_limit
from apps.usage.models import Feature, UsageReconciliation, UsageRecord, UsageReservation
from apps.usage.serializers import (
    UsageReconciliationSerializer,
    UsageRecordSerializer,
    UsageReservationSerializer,
)
from apps.usage.services import active_plan, current_period, feature_usage, period_spend
from apps.usage.tasks import usage_totals

# Frontend ``UsageByType`` buckets.
_BUCKETS = {
    "chat": {
        Feature.CHAT_MESSAGES,
        Feature.RAG_QUERIES,
        Feature.SEARCH_QUERIES,
        Feature.KNOWLEDGE_DOCUMENTS,
        Feature.API_CALLS,
    },
    "images": {Feature.IMAGE_GENERATIONS},
    "documents": {Feature.DOCUMENT_RENDERS, Feature.FILE_CONVERSIONS, Feature.ANALYSIS_RUNS},
    "agent": {Feature.AGENT_RUNS, Feature.WORKFLOW_EXECUTIONS},
}


def _decimal(value: Any) -> float:
    return float(Decimal(value or 0))


class UsageView(APIView):
    """Current-period usage, quotas, spending and concurrency for the selected tenant."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={
            200: inline_serializer(
                "TenantUsage",
                {
                    "totalCredits": serializers.FloatField(),
                    "byType": serializers.DictField(child=serializers.FloatField()),
                    "byFeature": serializers.ListField(child=serializers.DictField()),
                    "period": serializers.CharField(),
                    "plan": serializers.CharField(allow_null=True),
                    "quotas": serializers.ListField(child=serializers.DictField()),
                    "spending": serializers.DictField(),
                    "reservedCredits": serializers.FloatField(),
                    "concurrency": serializers.DictField(),
                },
            )
        }
    )
    def get(self, request: Request) -> Response:
        from apps.billing.models import CreditWallet, Entitlement

        organization = organization_for_request(request, required=True)
        period = current_period()
        records = UsageRecord.objects.filter(organization=organization, period=period)
        by_feature = list(
            records.values("feature")
            .annotate(credits=Sum("credits_charged"), units=Sum("quantity"))
            .order_by("feature")
        )
        totals = {row["feature"]: _decimal(row["credits"]) for row in by_feature}
        plan = active_plan(organization)
        quotas = []
        if plan is not None:
            for entitlement in Entitlement.objects.filter(plan=plan, feature__in=Feature.values):
                used = feature_usage(organization, entitlement.feature)
                limit = (
                    None
                    if entitlement.limit_type == Entitlement.LimitType.UNLIMITED
                    else entitlement.limit_value
                )
                quotas.append(
                    {
                        "feature": entitlement.feature,
                        "limitType": entitlement.limit_type,
                        "limit": _decimal(limit) if limit is not None else None,
                        "used": used,
                        "remaining": max(0.0, _decimal(limit) - used) if limit is not None else None,
                    }
                )
        wallet = CreditWallet.objects.filter(organization=organization).first()
        held = UsageReservation.objects.filter(
            organization=organization, status=UsageReservation.Status.HELD
        ).aggregate(total=Sum("credits_reserved"))["total"]
        return Response(
            {
                "totalCredits": sum(totals.values()),
                "byType": {
                    bucket: sum(totals.get(feature, 0.0) for feature in features)
                    for bucket, features in _BUCKETS.items()
                },
                "byFeature": [
                    {
                        "feature": row["feature"],
                        "credits": _decimal(row["credits"]),
                        "units": int(row["units"] or 0),
                    }
                    for row in by_feature
                ],
                "period": period,
                "plan": plan.slug if plan is not None else None,
                "quotas": quotas,
                "spending": {
                    "limit": _decimal(wallet.monthly_spending_limit)
                    if wallet and wallet.monthly_spending_limit is not None
                    else None,
                    "spentOrHeld": _decimal(period_spend(organization)),
                },
                "reservedCredits": _decimal(held),
                "concurrency": {
                    kind: {
                        "active": active_count(organization, kind),
                        "limit": concurrency_limit(organization, kind),
                    }
                    for kind in KINDS
                },
            }
        )


class UsageRecordViewSet(viewsets.ReadOnlyModelViewSet):
    """The tenant's immutable usage records (newest first)."""

    permission_classes = [IsAuthenticated]
    serializer_class = UsageRecordSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter("feature", str, required=False),
            OpenApiParameter("period", str, required=False, description="YYYY-MM"),
        ]
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return UsageRecord.objects.none()
        organization = organization_for_request(self.request, required=True)
        queryset = UsageRecord.objects.filter(organization=organization).order_by("-created_at")
        if feature := self.request.query_params.get("feature"):
            queryset = queryset.filter(feature=feature)
        if period := self.request.query_params.get("period"):
            queryset = queryset.filter(period=period)
        return queryset


def _date_range(request: Request) -> tuple[date, date]:
    today = timezone.now().date()
    try:
        start = date.fromisoformat(request.query_params.get("from") or str(today - timedelta(days=30)))
        end = date.fromisoformat(request.query_params.get("to") or str(today))
    except ValueError as exc:
        raise ValidationError({"from": "Use ISO dates (YYYY-MM-DD)."}) from exc
    if end < start or (end - start).days > 366:
        raise ValidationError({"to": "The range must be 0-366 days with from <= to."})
    return start, end


_RANGE_PARAMETERS = [
    OpenApiParameter("from", str, required=False, description="YYYY-MM-DD (default: 30 days ago)"),
    OpenApiParameter("to", str, required=False, description="YYYY-MM-DD (default: today)"),
]


class InternalUsageSummaryView(APIView):
    """Platform-wide usage, provider cost and margin (staff only)."""

    permission_classes = [IsAdminUser]

    @extend_schema(parameters=_RANGE_PARAMETERS, responses={200: OpenApiTypes.OBJECT})
    def get(self, request: Request) -> Response:
        start, end = _date_range(request)
        records = UsageRecord.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
        totals = usage_totals(records)
        credit_value = Decimal(str(settings.BILLING_CREDIT_VALUE_USD))
        revenue = Decimal(totals["credits"]) * credit_value
        cost = Decimal(totals["providerCostUsd"])
        held = UsageReservation.objects.filter(status=UsageReservation.Status.HELD).aggregate(
            credits=Sum("credits_reserved"), count=Count("id")
        )
        return Response(
            {
                "from": str(start),
                "to": str(end),
                "totals": totals,
                "revenueUsd": str(revenue),
                "providerCostUsd": str(cost),
                "grossMarginUsd": str(revenue - cost),
                "byFeature": list(
                    records.values("feature")
                    .annotate(
                        credits=Sum("credits_charged"),
                        costUsd=Sum("provider_cost_usd"),
                        units=Sum("quantity"),
                    )
                    .order_by("-credits")
                ),
                "byDay": [
                    {"date": str(row["day"]), "credits": str(row["credits"]), "costUsd": str(row["cost"])}
                    for row in records.annotate(day=TruncDate("created_at"))
                    .values("day")
                    .annotate(credits=Sum("credits_charged"), cost=Sum("provider_cost_usd"))
                    .order_by("day")
                ],
                "openReservations": {"count": held["count"] or 0, "credits": str(held["credits"] or 0)},
                "reconciliationDrifts": UsageReconciliation.objects.filter(
                    date__gte=start, date__lte=end, status=UsageReconciliation.Status.DRIFT
                ).count(),
            }
        )


class InternalUsageOrganizationsView(APIView):
    """Top organizations by credits charged in a date range (staff only)."""

    permission_classes = [IsAdminUser]

    @extend_schema(parameters=_RANGE_PARAMETERS, responses={200: OpenApiTypes.OBJECT})
    def get(self, request: Request) -> Response:
        start, end = _date_range(request)
        rows = (
            UsageRecord.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
            .values("organization_id", "organization__name")
            .annotate(
                credits=Sum("credits_charged"),
                uncollected=Sum("credits_uncollected"),
                costUsd=Sum("provider_cost_usd"),
                records=Count("id"),
                failedReconciliations=Count(
                    "organization__usage_reconciliations",
                    filter=Q(organization__usage_reconciliations__status=UsageReconciliation.Status.DRIFT),
                    distinct=True,
                ),
            )
            .order_by("-credits")[:100]
        )
        return Response(
            [
                {
                    "organizationId": str(row["organization_id"]),
                    "organizationName": row["organization__name"],
                    "credits": str(row["credits"] or 0),
                    "uncollectedCredits": str(row["uncollected"] or 0),
                    "providerCostUsd": str(row["costUsd"] or 0),
                    "records": row["records"],
                    "reconciliationDrifts": row["failedReconciliations"],
                }
                for row in rows
            ]
        )


class InternalReconciliationViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAdminUser]
    serializer_class = UsageReconciliationSerializer
    queryset = UsageReconciliation.objects.select_related("organization").order_by("-date", "provider")


class InternalReservationViewSet(viewsets.ReadOnlyModelViewSet):
    """Credit holds that are still open (operational view for stuck work)."""

    permission_classes = [IsAdminUser]
    serializer_class = UsageReservationSerializer
    queryset = UsageReservation.objects.filter(status=UsageReservation.Status.HELD).order_by("created_at")
