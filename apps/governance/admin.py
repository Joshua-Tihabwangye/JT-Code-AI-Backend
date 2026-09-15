from django.contrib import admin
from apps.governance.models import AuditEvent, ConsentRecord, RetentionRule, SafetyEvent, SupportCase


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ('id', 'organization', 'actor', 'category', 'action', 'resource_type', 'severity', 'created_at')
    list_filter = ('category', 'severity', 'created_at')
    search_fields = ('actor__email', 'organization__name', 'action', 'resource_type', 'resource_id', 'trace_id', 'request_id')
    readonly_fields = ('id', 'created_at')
    ordering = ('-created_at',)
    raw_id_fields = ('organization', 'actor')


@admin.register(ConsentRecord)
class ConsentRecordAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'organization', 'consent_type', 'status', 'version', 'granted_at', 'withdrawn_at')
    list_filter = ('consent_type', 'status', 'version')
    search_fields = ('user__email', 'organization__name')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('organization', 'user')


@admin.register(RetentionRule)
class RetentionRuleAdmin(admin.ModelAdmin):
    list_display = ('organization', 'data_category', 'action', 'retention_days', 'grace_period_days', 'is_active', 'legal_hold')
    list_filter = ('data_category', 'action', 'is_active', 'legal_hold')
    search_fields = ('organization__name',)
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('organization', 'data_category')
    raw_id_fields = ('organization', 'created_by')


@admin.register(SafetyEvent)
class SafetyEventAdmin(admin.ModelAdmin):
    list_display = ('id', 'organization', 'user', 'category', 'severity', 'action_taken', 'model_provider', 'model_name', 'created_at')
    list_filter = ('category', 'severity', 'action_taken', 'created_at')
    search_fields = ('user__email', 'organization__name', 'description', 'trace_id', 'request_id', 'prompt_hash')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('organization', 'user', 'job', 'reviewed_by')


@admin.register(SupportCase)
class SupportCaseAdmin(admin.ModelAdmin):
    list_display = ('id', 'subject', 'organization', 'user', 'status', 'priority', 'category', 'assigned_to', 'created_at')
    list_filter = ('status', 'priority', 'category', 'created_at')
    search_fields = ('subject', 'description', 'user__email', 'organization__name')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('organization', 'user', 'assigned_to')