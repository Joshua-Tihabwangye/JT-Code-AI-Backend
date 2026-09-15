from rest_framework import serializers

from apps.ai_gateway.models import Evaluation, Model, ModelPolicy, ModelRun, Prompt, Provider


class ProviderSerializer(serializers.ModelSerializer):
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    model_count = serializers.SerializerMethodField()

    class Meta:
        model = Provider
        fields = [
            'id', 'name', 'slug', 'type', 'type_display', 'status', 'status_display',
            'base_url', 'api_version', 'capabilities', 'supported_modalities',
            'rate_limits', 'regions', 'privacy_tier', 'default_headers',
            'timeout_seconds', 'max_retries', 'circuit_breaker_threshold',
            'model_count', 'metadata', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_model_count(self, obj):
        return obj.models.count()


class ModelSerializer(serializers.ModelSerializer):
    provider_name = serializers.CharField(source='provider.name', read_only=True)
    provider_slug = serializers.CharField(source='provider.slug', read_only=True)
    modality_display = serializers.CharField(source='get_modality_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Model
        fields = [
            'id', 'provider', 'provider_name', 'provider_slug', 'name', 'display_name',
            'modality', 'modality_display', 'status', 'status_display',
            'context_window', 'max_output_tokens', 'supports_tools',
            'supports_streaming', 'supports_json_mode', 'supports_vision',
            'input_price_per_token', 'output_price_per_token',
            'cached_input_price_per_token', 'image_price_per_unit',
            'audio_price_per_second', 'quality_score', 'latency_p50_ms',
            'latency_p99_ms', 'regions', 'retirement_date', 'tags', 'metadata',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ModelListSerializer(serializers.ModelSerializer):
    provider_name = serializers.CharField(source='provider.name', read_only=True)
    provider_slug = serializers.CharField(source='provider.slug', read_only=True)

    class Meta:
        model = Model
        fields = [
            'id', 'provider', 'provider_name', 'provider_slug', 'name', 'display_name',
            'modality', 'status', 'context_window', 'max_output_tokens',
            'supports_tools', 'supports_streaming', 'supports_vision',
            'input_price_per_token', 'output_price_per_token', 'quality_score',
            'latency_p50_ms'
        ]


class ModelPolicySerializer(serializers.ModelSerializer):
    primary_model_name = serializers.CharField(source='primary_model.display_name', read_only=True)
    primary_model_slug = serializers.CharField(source='primary_model.name', read_only=True)
    fallback_model_names = serializers.SerializerMethodField()
    routing_strategy_display = serializers.CharField(source='get_routing_strategy_display', read_only=True)
    fallback_policy_display = serializers.CharField(source='get_fallback_policy_display', read_only=True)

    class Meta:
        model = ModelPolicy
        fields = [
            'id', 'name', 'slug', 'description', 'task_type', 'routing_strategy',
            'routing_strategy_display', 'primary_model', 'primary_model_name',
            'primary_model_slug', 'fallback_models', 'fallback_model_names',
            'fallback_policy', 'fallback_policy_display', 'max_cost_usd',
            'max_latency_ms', 'min_quality_score', 'allowed_providers',
            'blocked_providers', 'allowed_regions', 'required_capabilities',
            'plan_restrictions', 'is_default', 'is_active', 'version', 'metadata',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_fallback_model_names(self, obj):
        return list(obj.fallback_models.values_list('name', flat=True))


class ModelRunSerializer(serializers.ModelSerializer):
    provider_name = serializers.CharField(source='provider.name', read_only=True)
    model_name = serializers.CharField(source='model.display_name', read_only=True)
    model_slug = serializers.CharField(source='model.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = ModelRun
        fields = [
            'id', 'request_id', 'job_id', 'job_step_id', 'provider', 'provider_name',
            'model', 'model_name', 'model_slug', 'policy', 'status', 'status_display',
            'input_tokens', 'output_tokens', 'cached_tokens', 'reasoning_tokens',
            'image_count', 'audio_seconds', 'provider_cost_usd', 'estimated_cost_usd',
            'latency_ms', 'time_to_first_token_ms', 'trace_id', 'error_code',
            'error_message', 'retry_count', 'fallback_used', 'metadata',
            'created_at', 'completed_at'
        ]
        read_only_fields = ['id', 'created_at', 'completed_at']


class PromptSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)

    class Meta:
        model = Prompt
        fields = [
            'id', 'name', 'slug', 'category', 'category_display', 'content',
            'variables', 'description', 'model_constraints', 'version',
            'is_active', 'tags', 'metadata', 'created_by', 'created_by_email',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'version', 'created_at', 'updated_at']


class PromptCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Prompt
        fields = [
            'name', 'slug', 'category', 'content', 'variables', 'description',
            'model_constraints', 'tags',
        ]


class EvaluationSerializer(serializers.ModelSerializer):
    model_name = serializers.CharField(source='model.display_name', read_only=True)
    prompt_name = serializers.CharField(source='prompt.name', read_only=True)
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_email = serializers.EmailField(source='created_by.email', read_only=True)

    class Meta:
        model = Evaluation
        fields = [
            'id', 'name', 'slug', 'type', 'type_display', 'status', 'status_display',
            'model', 'model_name', 'prompt', 'prompt_name', 'dataset_name',
            'dataset_version', 'metrics', 'threshold_passed', 'results_summary',
            'error_message', 'run_id', 'metadata', 'created_by', 'created_by_email',
            'created_at', 'updated_at', 'completed_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'completed_at']


class EvaluationCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Evaluation
        fields = ['name', 'slug', 'type', 'model', 'prompt', 'dataset_name', 'dataset_version', 'metadata']