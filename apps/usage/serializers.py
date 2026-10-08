from rest_framework import serializers

from apps.usage.models import UsageReconciliation, UsageRecord, UsageReservation


class UsageRecordSerializer(serializers.ModelSerializer):
    sourceType = serializers.CharField(source="source_type")
    sourceId = serializers.CharField(source="source_id")
    creditsCharged = serializers.DecimalField(source="credits_charged", max_digits=20, decimal_places=6)
    creditsUncollected = serializers.DecimalField(
        source="credits_uncollected", max_digits=20, decimal_places=6
    )
    providerCostUsd = serializers.DecimalField(source="provider_cost_usd", max_digits=20, decimal_places=8)
    inputTokens = serializers.IntegerField(source="input_tokens")
    outputTokens = serializers.IntegerField(source="output_tokens")
    modelRunCount = serializers.IntegerField(source="model_run_count")
    createdAt = serializers.DateTimeField(source="created_at")

    class Meta:
        model = UsageRecord
        fields = [
            "id",
            "feature",
            "quantity",
            "sourceType",
            "sourceId",
            "basis",
            "creditsCharged",
            "creditsUncollected",
            "providerCostUsd",
            "inputTokens",
            "outputTokens",
            "modelRunCount",
            "pricing",
            "period",
            "createdAt",
        ]
        read_only_fields = fields


class UsageReservationSerializer(serializers.ModelSerializer):
    organizationId = serializers.UUIDField(source="organization_id")
    sourceType = serializers.CharField(source="source_type")
    sourceId = serializers.CharField(source="source_id")
    creditsReserved = serializers.DecimalField(source="credits_reserved", max_digits=20, decimal_places=6)
    expiresAt = serializers.DateTimeField(source="expires_at")
    createdAt = serializers.DateTimeField(source="created_at")

    class Meta:
        model = UsageReservation
        fields = [
            "id",
            "organizationId",
            "feature",
            "quantity",
            "creditsReserved",
            "sourceType",
            "sourceId",
            "status",
            "expiresAt",
            "createdAt",
        ]
        read_only_fields = fields


class UsageReconciliationSerializer(serializers.ModelSerializer):
    organizationId = serializers.UUIDField(source="organization_id", allow_null=True)
    modelRuns = serializers.IntegerField(source="model_runs")
    modelRunCostUsd = serializers.DecimalField(source="model_run_cost_usd", max_digits=20, decimal_places=8)
    recomputedCostUsd = serializers.DecimalField(
        source="recomputed_cost_usd", max_digits=20, decimal_places=8
    )
    billedCostUsd = serializers.DecimalField(source="billed_cost_usd", max_digits=20, decimal_places=8)
    unbilledRuns = serializers.IntegerField(source="unbilled_runs")
    unbilledCostUsd = serializers.DecimalField(source="unbilled_cost_usd", max_digits=20, decimal_places=8)
    providerReportedCostUsd = serializers.DecimalField(
        source="provider_reported_cost_usd", max_digits=20, decimal_places=8, allow_null=True
    )

    class Meta:
        model = UsageReconciliation
        fields = [
            "id",
            "date",
            "organizationId",
            "provider",
            "modelRuns",
            "modelRunCostUsd",
            "recomputedCostUsd",
            "billedCostUsd",
            "unbilledRuns",
            "unbilledCostUsd",
            "providerReportedCostUsd",
            "status",
            "details",
        ]
        read_only_fields = fields
