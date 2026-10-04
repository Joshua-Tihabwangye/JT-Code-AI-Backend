from django.contrib import admin

from apps.usage.models import UsageReconciliation, UsageRecord, UsageReservation


@admin.register(UsageRecord)
class UsageRecordAdmin(admin.ModelAdmin):
    """Read-only: usage records are append-only (enforced in the database)."""

    list_display = (
        "created_at",
        "organization",
        "feature",
        "quantity",
        "credits_charged",
        "provider_cost_usd",
    )
    list_filter = ("feature", "basis", "period")
    search_fields = ("source_id", "organization__name")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(UsageReservation)
class UsageReservationAdmin(admin.ModelAdmin):
    list_display = ("created_at", "organization", "feature", "credits_reserved", "status", "expires_at")
    list_filter = ("status", "feature")
    search_fields = ("source_id", "organization__name")
    readonly_fields = [field.name for field in UsageReservation._meta.fields]


@admin.register(UsageReconciliation)
class UsageReconciliationAdmin(admin.ModelAdmin):
    list_display = ("date", "organization", "provider", "status", "model_run_cost_usd", "unbilled_cost_usd")
    list_filter = ("status", "provider", "date")
    readonly_fields = [field.name for field in UsageReconciliation._meta.fields]
