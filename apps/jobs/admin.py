from django.contrib import admin
from apps.jobs.models import Job, JobStep, ProviderAttempt, WorkflowRun, Callback


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ('id', 'owner', 'task_type', 'status', 'reserved_credits', 'created_at')
    list_filter = ('status', 'task_type', 'created_at')
    search_fields = ('owner__email', 'request_id', 'idempotency_key', 'trace_id')
    readonly_fields = ('id', 'request_id', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('owner', 'organization', 'conversation')


@admin.register(JobStep)
class JobStepAdmin(admin.ModelAdmin):
    list_display = ('id', 'job', 'name', 'step_order', 'status', 'provider', 'model', 'created_at')
    list_filter = ('status', 'provider', 'created_at')
    search_fields = ('job__id', 'name', 'provider', 'model')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('job', 'step_order')
    raw_id_fields = ('job',)


@admin.register(ProviderAttempt)
class ProviderAttemptAdmin(admin.ModelAdmin):
    list_display = ('id', 'job_step', 'attempt_number', 'provider', 'model', 'status', 'cost_usd', 'created_at')
    list_filter = ('status', 'provider', 'created_at')
    search_fields = ('job_step__id', 'provider', 'model', 'trace_id')
    readonly_fields = ('id', 'created_at', 'completed_at')
    ordering = ('job_step', 'attempt_number')
    raw_id_fields = ('job_step',)


@admin.register(WorkflowRun)
class WorkflowRunAdmin(admin.ModelAdmin):
    list_display = ('id', 'job', 'n8n_workflow_id', 'status', 'progress_percent', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('job__id', 'n8n_workflow_id', 'n8n_execution_id')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('job',)


@admin.register(Callback)
class CallbackAdmin(admin.ModelAdmin):
    list_display = ('id', 'job', 'url', 'status', 'attempts', 'next_retry_at', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('job__id', 'url')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    raw_id_fields = ('job',)