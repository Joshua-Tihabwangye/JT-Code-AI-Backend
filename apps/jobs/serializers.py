from rest_framework import serializers
from apps.jobs.models import Job, JobStep, ProviderAttempt, WorkflowRun, Callback


class ProviderAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProviderAttempt
        fields = [
            'id', 'attempt_number', 'provider', 'model', 'status',
            'input_payload', 'output_payload', 'input_tokens', 'output_tokens',
            'cost_usd', 'latency_ms', 'error_code', 'error_message',
            'policy_version', 'trace_id', 'created_at', 'completed_at'
        ]
        read_only_fields = ['id', 'created_at', 'completed_at']


class JobStepSerializer(serializers.ModelSerializer):
    provider_attempts = ProviderAttemptSerializer(many=True, read_only=True)

    class Meta:
        model = JobStep
        fields = [
            'id', 'name', 'step_order', 'status',
            'input_payload', 'output_payload', 'provider', 'model',
            'estimated_cost_usd', 'actual_cost_usd',
            'input_tokens', 'output_tokens', 'error_message',
            'started_at', 'completed_at', 'created_at', 'updated_at',
            'provider_attempts'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class WorkflowRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowRun
        fields = [
            'id', 'n8n_workflow_id', 'n8n_execution_id', 'status',
            'input_payload', 'output_payload', 'steps_completed',
            'total_steps', 'progress_percent', 'error_message',
            'started_at', 'completed_at', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class CallbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = Callback
        fields = [
            'id', 'url', 'payload', 'status', 'attempts', 'max_attempts',
            'last_attempt_at', 'last_error', 'response_status', 'response_body',
            'next_retry_at', 'expires_at', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class JobSerializer(serializers.ModelSerializer):
    steps = JobStepSerializer(many=True, read_only=True)
    workflow_run = WorkflowRunSerializer(read_only=True)
    callbacks = CallbackSerializer(many=True, read_only=True)
    owner_email = serializers.EmailField(source='owner.email', read_only=True)
    organization_name = serializers.CharField(source='organization.name', read_only=True)
    conversation_title = serializers.CharField(source='conversation.title', read_only=True)

    class Meta:
        model = Job
        fields = [
            'id', 'owner', 'owner_email', 'organization', 'organization_name',
            'conversation', 'conversation_title', 'request_id', 'idempotency_key',
            'task_type', 'status', 'input_payload', 'entitlement_snapshot',
            'reserved_credits', 'actual_credits', 'result', 'error_code',
            'error_message', 'trace_id', 'n8n_workflow_id', 'n8n_execution_id',
            'callback_url', 'deadline', 'started_at', 'completed_at',
            'created_at', 'updated_at', 'steps', 'workflow_run', 'callbacks'
        ]
        read_only_fields = [
            'id', 'request_id', 'owner', 'organization', 'reserved_credits',
            'actual_credits', 'result', 'error_code', 'error_message',
            'trace_id', 'n8n_workflow_id', 'n8n_execution_id',
            'started_at', 'completed_at', 'created_at', 'updated_at'
        ]


class JobCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Job
        fields = [
            'idempotency_key', 'task_type', 'input_payload', 'callback_url', 'deadline'
        ]
        extra_kwargs = {
            'idempotency_key': {'required': True},
            'task_type': {'required': True},
            'input_payload': {'required': True},
        }

    def validate_task_type(self, value):
        valid_types = [choice[0] for choice in Job.TaskType.choices]
        if value not in valid_types:
            raise serializers.ValidationError(f'Invalid task_type. Must be one of: {valid_types}')
        return value


class JobStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Job.Status.choices)
    result = serializers.JSONField(required=False)
    error_code = serializers.CharField(required=False, allow_blank=True)
    error_message = serializers.CharField(required=False, allow_blank=True)
    n8n_execution_id = serializers.CharField(required=False, allow_blank=True)
    progress_percent = serializers.IntegerField(required=False, min_value=0, max_value=100)
    steps_completed = serializers.IntegerField(required=False, min_value=0)
    total_steps = serializers.IntegerField(required=False, min_value=0)