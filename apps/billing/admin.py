from django.contrib import admin
from apps.billing.models import Plan, Entitlement, Subscription, CreditWallet, CreditLedger, Invoice, Payment


class EntitlementInline(admin.TabularInline):
    model = Entitlement
    extra = 0
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'price_cents', 'currency', 'interval', 'status', 'monthly_credits', 'is_popular', 'sort_order')
    list_filter = ('status', 'interval', 'is_popular')
    search_fields = ('name', 'slug')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('sort_order', 'name')
    inlines = [EntitlementInline]


@admin.register(Entitlement)
class EntitlementAdmin(admin.ModelAdmin):
    list_display = ('plan', 'feature', 'limit_type', 'limit_value', 'reset_period', 'overage_price_cents')
    list_filter = ('limit_type', 'reset_period')
    search_fields = ('plan__name', 'feature')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('plan', 'feature')


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ('id', 'organization', 'plan', 'status', 'provider', 'current_period_end', 'cancel_at_period_end')
    list_filter = ('status', 'provider', 'plan')
    search_fields = ('organization__name', 'provider_subscription_id', 'provider_customer_id')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('organization', 'plan')


@admin.register(CreditWallet)
class CreditWalletAdmin(admin.ModelAdmin):
    list_display = ('organization', 'balance', 'reserved_balance', 'available_balance', 'currency', 'auto_topup_enabled')
    search_fields = ('organization__name',)
    readonly_fields = ('id', 'created_at', 'updated_at')
    raw_id_fields = ('organization',)


@admin.register(CreditLedger)
class CreditLedgerAdmin(admin.ModelAdmin):
    list_display = ('id', 'wallet', 'direction', 'credits', 'reason', 'balance_after', 'created_at')
    list_filter = ('direction', 'reason', 'created_at')
    search_fields = ('wallet__organization__name', 'request_id', 'job_id', 'idempotency_key')
    readonly_fields = ('id', 'created_at')
    ordering = ('-created_at',)
    raw_id_fields = ('wallet',)


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('id', 'organization', 'provider_invoice_id', 'status', 'amount_cents', 'currency', 'period_end', 'paid_at')
    list_filter = ('status', 'provider', 'currency')
    search_fields = ('organization__name', 'provider_invoice_id')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('organization', 'subscription')


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('id', 'organization', 'provider_payment_id', 'type', 'status', 'amount_cents', 'currency', 'credits_granted', 'created_at')
    list_filter = ('type', 'status', 'provider', 'currency')
    search_fields = ('organization__name', 'provider_payment_id', 'idempotency_key')
    readonly_fields = ('id', 'created_at', 'updated_at', 'succeeded_at')
    ordering = ('-created_at',)
    raw_id_fields = ('organization', 'invoice', 'wallet')