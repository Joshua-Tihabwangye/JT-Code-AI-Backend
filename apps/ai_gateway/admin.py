from django.contrib import admin

from apps.ai_gateway.models import Evaluation, Model, ModelPolicy, ModelRun, Prompt, Provider


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'type', 'status', 'base_url', 'api_version', 'created_at')
    list_filter = ('type', 'status', 'created_at')
    search_fields = ('name', 'slug', 'base_url')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('name',)


@admin.register(Model)
class ModelAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'display_name', 'provider', 'modality', 'status', 'context_window',
        'max_output_tokens', 'input_price_per_token', 'output_price_per_token',
    )
    list_filter = (
        'modality', 'status', 'supports_tools', 'supports_streaming',
        'supports_vision', 'provider',
    )
    search_fields = ('name', 'display_name', 'provider__name')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('provider', 'name')
    raw_id_fields = ('provider',)


@admin.register(ModelPolicy)
class ModelPolicyAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'slug', 'task_type', 'routing_strategy', 'primary_model',
        'fallback_policy', 'is_default', 'is_active', 'version',
    )
    list_filter = ('routing_strategy', 'fallback_policy', 'is_default', 'is_active', 'created_at')
    search_fields = ('name', 'slug', 'task_type')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('task_type', 'name')
    raw_id_fields = ('primary_model',)
    filter_horizontal = ('fallback_models', 'allowed_providers', 'blocked_providers')


@admin.register(ModelRun)
class ModelRunAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'request_id', 'provider', 'model', 'status', 'input_tokens',
        'output_tokens', 'provider_cost_usd', 'latency_ms', 'created_at',
    )
    list_filter = ('status', 'provider', 'model', 'fallback_used', 'created_at')
    search_fields = ('request_id', 'job_id', 'trace_id', 'provider__name', 'model__name')
    readonly_fields = ('id', 'created_at', 'completed_at')
    ordering = ('-created_at',)
    raw_id_fields = ('provider', 'model', 'policy')


@admin.register(Prompt)
class PromptAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'category', 'version', 'is_active', 'created_at')
    list_filter = ('category', 'is_active', 'version', 'created_at')
    search_fields = ('name', 'slug', 'content')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('category', 'name')
    raw_id_fields = ('created_by',)


@admin.register(Evaluation)
class EvaluationAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'type', 'status', 'model', 'prompt', 'threshold_passed', 'created_at')
    list_filter = ('type', 'status', 'threshold_passed', 'created_at')
    search_fields = ('name', 'slug', 'model__name', 'prompt__name', 'dataset_name')
    readonly_fields = ('id', 'created_at', 'updated_at', 'completed_at')
    ordering = ('-created_at',)
    raw_id_fields = ('model', 'prompt', 'created_by')