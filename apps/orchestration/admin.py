from django.contrib import admin

from apps.orchestration.models import Automation, WorkflowCallback, WorkflowDefinition, WorkflowEventDelivery


@admin.register(WorkflowDefinition)
class WorkflowDefinitionAdmin(admin.ModelAdmin):
    list_display = ("key", "version", "kind", "is_active", "n8n_workflow_id", "n8n_active", "synced_at")
    list_filter = ("kind", "is_active", "n8n_active")
    search_fields = ("key", "name", "n8n_workflow_id")
    readonly_fields = [field.name for field in WorkflowDefinition._meta.fields]


@admin.register(WorkflowEventDelivery)
class WorkflowEventDeliveryAdmin(admin.ModelAdmin):
    list_display = ("id", "event_type", "definition", "status", "attempts", "next_attempt_at", "created_at")
    list_filter = ("status", "event_type")
    search_fields = ("id", "event_id", "n8n_execution_id")
    raw_id_fields = ("organization", "definition")


@admin.register(WorkflowCallback)
class WorkflowCallbackAdmin(admin.ModelAdmin):
    list_display = ("kind", "target_id", "received_at")
    list_filter = ("kind",)
    search_fields = ("target_id", "nonce")


@admin.register(Automation)
class AutomationAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "schedule", "is_active", "next_run_at", "run_count")
    list_filter = ("is_active",)
    search_fields = ("name", "organization__name")
    raw_id_fields = ("organization", "created_by", "last_job")
