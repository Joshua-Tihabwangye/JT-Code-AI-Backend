from rest_framework import serializers
from apps.billing.models import Plan, Entitlement, Subscription, CreditWallet, CreditLedger, Invoice, Payment


class EntitlementSerializer(serializers.ModelSerializer):
    feature_display = serializers.CharField(source='get_feature_display', read_only=True)
    limit_type_display = serializers.CharField(source='get_limit_type_display', read_only=True)

    class Meta:
        model = Entitlement
        fields = [
            'id', 'feature', 'feature_display', 'limit_type', 'limit_type_display',
            'limit_value', 'reset_period', 'overage_price_cents', 'metadata'
        ]
        read_only_fields = ['id']


class PlanSerializer(serializers.ModelSerializer):
    entitlements = EntitlementSerializer(many=True, read_only=True)
    price_dollars = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = [
            'id', 'name', 'slug', 'description', 'price_cents', 'price_dollars',
            'currency', 'interval', 'status', 'features', 'limits',
            'credit_value_usd', 'monthly_credits', 'is_popular', 'sort_order',
            'metadata', 'entitlements', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_price_dollars(self, obj):
        return obj.price_cents / 100


class PlanListSerializer(serializers.ModelSerializer):
    price_dollars = serializers.SerializerMethodField()
    entitlement_summary = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = [
            'id', 'name', 'slug', 'description', 'price_cents', 'price_dollars',
            'currency', 'interval', 'status', 'monthly_credits', 'is_popular',
            'sort_order', 'entitlement_summary'
        ]

    def get_price_dollars(self, obj):
        return obj.price_cents / 100

    def get_entitlement_summary(self, obj):
        return {
            e.feature: {
                'limit_type': e.limit_type,
                'limit_value': float(e.limit_value) if e.limit_value else None,
                'reset_period': e.reset_period
            }
            for e in obj.entitlements.all()
        }


class SubscriptionSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    plan_slug = serializers.CharField(source='plan.slug', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    is_active = serializers.SerializerMethodField()
    days_remaining = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = [
            'id', 'organization', 'organization_name', 'plan', 'plan_name', 'plan_slug',
            'status', 'provider', 'provider_subscription_id', 'provider_customer_id',
            'current_period_start', 'current_period_end', 'cancel_at_period_end',
            'canceled_at', 'trial_start', 'trial_end', 'quantity', 'metadata',
            'is_active', 'days_remaining', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'provider_subscription_id', 'provider_customer_id',
            'current_period_start', 'current_period_end', 'canceled_at',
            'created_at', 'updated_at'
        ]

    def get_is_active(self, obj):
        return obj.status in ['active', 'trialing']

    def get_days_remaining(self, obj):
        from django.utils import timezone
        if obj.current_period_end:
            delta = obj.current_period_end - timezone.now()
            return max(0, delta.days)
        return None


class CreditWalletSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    available_balance = serializers.DecimalField(max_digits=20, decimal_places=6, read_only=True)

    class Meta:
        model = CreditWallet
        fields = [
            'id', 'organization', 'organization_name', 'balance', 'reserved_balance',
            'available_balance', 'currency', 'credit_value_usd',
            'auto_topup_enabled', 'auto_topup_threshold', 'auto_topup_amount',
            'last_topup_at', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'balance', 'reserved_balance', 'last_topup_at',
            'created_at', 'updated_at'
        ]


class CreditLedgerSerializer(serializers.ModelSerializer):
    wallet_organization = serializers.CharField(source='wallet.organization.name', read_only=True)
    direction_display = serializers.CharField(source='get_direction_display', read_only=True)
    reason_display = serializers.CharField(source='get_reason_display', read_only=True)

    class Meta:
        model = CreditLedger
        fields = [
            'id', 'wallet', 'wallet_organization', 'direction', 'direction_display',
            'credits', 'reason', 'reason_display', 'description', 'request_id',
            'job_id', 'idempotency_key', 'price_snapshot', 'balance_after',
            'metadata', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class InvoiceSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    subscription_plan = serializers.CharField(source='subscription.plan.name', read_only=True)
    amount_dollars = serializers.SerializerMethodField()
    amount_paid_dollars = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            'id', 'organization', 'organization_name', 'subscription', 'subscription_plan',
            'provider', 'provider_invoice_id', 'status', 'amount_cents', 'amount_dollars',
            'amount_paid_cents', 'amount_paid_dollars', 'currency',
            'period_start', 'period_end', 'due_date', 'paid_at',
            'invoice_pdf_url', 'hosted_invoice_url', 'metadata',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'provider_invoice_id', 'amount_paid_cents', 'paid_at',
            'invoice_pdf_url', 'hosted_invoice_url', 'created_at', 'updated_at'
        ]

    def get_amount_dollars(self, obj):
        return obj.amount_cents / 100

    def get_amount_paid_dollars(self, obj):
        return obj.amount_paid_cents / 100


class PaymentSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    amount_dollars = serializers.SerializerMethodField()
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Payment
        fields = [
            'id', 'organization', 'organization_name', 'invoice', 'wallet',
            'provider', 'provider_payment_id', 'type', 'type_display', 'status',
            'status_display', 'amount_cents', 'amount_dollars', 'currency',
            'credits_granted', 'idempotency_key', 'failure_code',
            'failure_message', 'receipt_url', 'metadata',
            'created_at', 'updated_at', 'succeeded_at'
        ]
        read_only_fields = [
            'id', 'provider_payment_id', 'credits_granted',
            'created_at', 'updated_at', 'succeeded_at'
        ]

    def get_amount_dollars(self, obj):
        return obj.amount_cents / 100


class TopUpSerializer(serializers.Serializer):
    amount_cents = serializers.IntegerField(min_value=100)
    payment_method_id = serializers.CharField(required=False)
    idempotency_key = serializers.CharField(required=False)

    def validate_amount_cents(self, value):
        if value < 100:
            raise serializers.ValidationError('Minimum top-up amount is $1.00')
        return value