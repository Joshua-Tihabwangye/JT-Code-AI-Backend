"""Billing API serializers (camelCase, matching the frontend billing contract)."""

from __future__ import annotations

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.billing.models import CreditLedger, CreditWallet, Entitlement, Invoice, Payment, Plan, Subscription


class EntitlementSerializer(serializers.ModelSerializer):
    limitType = serializers.CharField(source="limit_type")
    limitValue = serializers.DecimalField(
        source="limit_value", max_digits=20, decimal_places=6, allow_null=True
    )
    resetPeriod = serializers.CharField(source="reset_period")

    class Meta:
        model = Entitlement
        fields = ["feature", "limitType", "limitValue", "resetPeriod"]
        read_only_fields = fields


class PlanSerializer(serializers.ModelSerializer):
    priceCents = serializers.IntegerField(source="price_cents")
    priceYearlyCents = serializers.IntegerField(source="price_yearly_cents")
    interval = serializers.SerializerMethodField()
    monthlyCredits = serializers.DecimalField(
        source="monthly_credits", max_digits=20, decimal_places=6, coerce_to_string=False
    )
    isPopular = serializers.BooleanField(source="is_popular")
    features = serializers.SerializerMethodField()
    limits = serializers.JSONField()
    entitlements = EntitlementSerializer(many=True, read_only=True)
    availableIntervals = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "priceCents",
            "priceYearlyCents",
            "currency",
            "interval",
            "monthlyCredits",
            "isPopular",
            "features",
            "limits",
            "entitlements",
            "availableIntervals",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.ChoiceField(choices=["month", "year"]))
    def get_interval(self, obj: Plan) -> str:
        return "year" if obj.interval == Plan.Interval.YEARLY else "month"

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_features(self, obj: Plan) -> list[str]:
        if isinstance(obj.features, list):
            return [str(item) for item in obj.features]
        return [str(item) for item in (obj.features or {}).get("highlights", [])]

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_availableIntervals(self, obj: Plan) -> list[str]:
        intervals = []
        if obj.stripe_price_monthly_id:
            intervals.append("month")
        if obj.stripe_price_yearly_id:
            intervals.append("year")
        return intervals


class SubscriptionSerializer(serializers.ModelSerializer):
    plan = serializers.CharField(source="plan.slug")
    planName = serializers.CharField(source="plan.name")
    currentPeriodStart = serializers.DateTimeField(source="current_period_start")
    currentPeriodEnd = serializers.DateTimeField(source="current_period_end")
    cancelAtPeriodEnd = serializers.BooleanField(source="cancel_at_period_end")
    canceledAt = serializers.DateTimeField(source="canceled_at", allow_null=True)
    isActive = serializers.SerializerMethodField()
    daysRemaining = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = [
            "id",
            "status",
            "plan",
            "planName",
            "interval",
            "currentPeriodStart",
            "currentPeriodEnd",
            "cancelAtPeriodEnd",
            "canceledAt",
            "isActive",
            "daysRemaining",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.BooleanField())
    def get_isActive(self, obj: Subscription) -> bool:
        return obj.status in {Subscription.Status.ACTIVE, Subscription.Status.TRIALING}

    @extend_schema_field(serializers.IntegerField())
    def get_daysRemaining(self, obj: Subscription) -> int:
        return max(0, (obj.current_period_end - timezone.now()).days)


class CreditWalletSerializer(serializers.ModelSerializer):
    balance = serializers.DecimalField(max_digits=20, decimal_places=6, coerce_to_string=False)
    reservedBalance = serializers.DecimalField(
        source="reserved_balance", max_digits=20, decimal_places=6, coerce_to_string=False
    )
    availableBalance = serializers.DecimalField(
        source="available_balance", max_digits=20, decimal_places=6, coerce_to_string=False
    )
    creditValueUsd = serializers.DecimalField(
        source="credit_value_usd", max_digits=10, decimal_places=6, coerce_to_string=False
    )
    autoTopupEnabled = serializers.BooleanField(source="auto_topup_enabled")
    autoTopupThreshold = serializers.DecimalField(
        source="auto_topup_threshold", max_digits=20, decimal_places=6, coerce_to_string=False
    )
    autoTopupAmountCents = serializers.SerializerMethodField()
    monthlySpendingLimit = serializers.DecimalField(
        source="monthly_spending_limit",
        max_digits=20,
        decimal_places=6,
        allow_null=True,
        coerce_to_string=False,
    )
    lastTopupAt = serializers.DateTimeField(source="last_topup_at", allow_null=True)

    class Meta:
        model = CreditWallet
        fields = [
            "id",
            "balance",
            "reservedBalance",
            "availableBalance",
            "currency",
            "creditValueUsd",
            "autoTopupEnabled",
            "autoTopupThreshold",
            "autoTopupAmountCents",
            "monthlySpendingLimit",
            "lastTopupAt",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.IntegerField())
    def get_autoTopupAmountCents(self, obj: CreditWallet) -> int:
        return int(obj.auto_topup_amount * obj.credit_value_usd * 100)


class WalletSettingsSerializer(serializers.Serializer):
    """Accepts the frontend's snake_case keys and their camelCase forms."""

    auto_topup_enabled = serializers.BooleanField(required=False)
    auto_topup_threshold = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=0, required=False
    )
    auto_topup_amount_cents = serializers.IntegerField(min_value=1, required=False)
    monthly_spending_limit = serializers.DecimalField(
        max_digits=20, decimal_places=6, min_value=0, required=False, allow_null=True
    )

    def to_internal_value(self, data):
        aliases = {
            "autoTopupEnabled": "auto_topup_enabled",
            "autoTopupThreshold": "auto_topup_threshold",
            "autoTopupAmountCents": "auto_topup_amount_cents",
            "monthlySpendingLimit": "monthly_spending_limit",
        }
        normalized = {aliases.get(key, key): value for key, value in dict(data).items()}
        return super().to_internal_value(normalized)


class CreditLedgerSerializer(serializers.ModelSerializer):
    balanceAfter = serializers.DecimalField(source="balance_after", max_digits=20, decimal_places=6)
    createdAt = serializers.DateTimeField(source="created_at")

    class Meta:
        model = CreditLedger
        fields = [
            "id",
            "direction",
            "credits",
            "reason",
            "description",
            "balanceAfter",
            "metadata",
            "createdAt",
        ]
        read_only_fields = fields


class InvoiceSerializer(serializers.ModelSerializer):
    date = serializers.DateTimeField(source="created_at")
    amountCents = serializers.IntegerField(source="amount_cents")
    amountPaidCents = serializers.IntegerField(source="amount_paid_cents")
    periodStart = serializers.DateTimeField(source="period_start")
    periodEnd = serializers.DateTimeField(source="period_end")
    paidAt = serializers.DateTimeField(source="paid_at", allow_null=True)
    hostedInvoiceUrl = serializers.CharField(source="hosted_invoice_url")
    hasPdf = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            "id",
            "number",
            "date",
            "amountCents",
            "amountPaidCents",
            "currency",
            "status",
            "description",
            "periodStart",
            "periodEnd",
            "paidAt",
            "hostedInvoiceUrl",
            "hasPdf",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.BooleanField())
    def get_hasPdf(self, obj: Invoice) -> bool:
        return bool(obj.invoice_pdf_url)


class PaymentSerializer(serializers.ModelSerializer):
    amountCents = serializers.IntegerField(source="amount_cents")
    creditsGranted = serializers.DecimalField(source="credits_granted", max_digits=20, decimal_places=6)
    refundedCents = serializers.IntegerField(source="refunded_cents")
    creditsReversed = serializers.DecimalField(source="credits_reversed", max_digits=20, decimal_places=6)
    failureMessage = serializers.CharField(source="failure_message")
    createdAt = serializers.DateTimeField(source="created_at")
    succeededAt = serializers.DateTimeField(source="succeeded_at", allow_null=True)

    class Meta:
        model = Payment
        fields = [
            "id",
            "type",
            "status",
            "amountCents",
            "currency",
            "creditsGranted",
            "refundedCents",
            "creditsReversed",
            "failureMessage",
            "createdAt",
            "succeededAt",
        ]
        read_only_fields = fields


class TopUpSerializer(serializers.Serializer):
    amount_cents = serializers.IntegerField(min_value=1)


class SubscribeSerializer(serializers.Serializer):
    interval = serializers.ChoiceField(choices=["month", "year"], default="month")
    successUrl = serializers.URLField(required=False)
    cancelUrl = serializers.URLField(required=False)


class PortalSerializer(serializers.Serializer):
    returnUrl = serializers.URLField(required=False)
