from rest_framework import serializers

from apps.jobs.models import Callback, Job, JobStep, ProviderAttempt, WorkflowRun
from apps.jobs.webhooks import validate_callback_url


class ProviderAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProviderAttempt
        fields = [
            "id",
            "attempt_number",
            "provider",
            "model",
            "status",
            "input_payload",
            "output_payload",
            "input_tokens",
            "output_tokens",
            "cost_usd",
            "latency_ms",
            "error_code",
            "error_message",
            "policy_version",
            "trace_id",
            "created_at",
            "completed_at",
        ]
        read_only_fields = ["id", "created_at", "completed_at"]


class JobStepSerializer(serializers.ModelSerializer):
    provider_attempts = ProviderAttemptSerializer(many=True, read_only=True)

    class Meta:
        model = JobStep
        fields = [
            "id",
            "name",
            "step_order",
            "status",
            "input_payload",
            "output_payload",
            "provider",
            "model",
            "estimated_cost_usd",
            "actual_cost_usd",
            "input_tokens",
            "output_tokens",
            "error_message",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
            "provider_attempts",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class WorkflowRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowRun
        fields = [
            "id",
            "n8n_workflow_id",
            "n8n_execution_id",
            "status",
            "input_payload",
            "output_payload",
            "steps_completed",
            "total_steps",
            "progress_percent",
            "error_message",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class CallbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = Callback
        fields = [
            "id",
            "url",
            "payload",
            "status",
            "attempts",
            "max_attempts",
            "last_attempt_at",
            "last_error",
            "response_status",
            "response_body",
            "next_retry_at",
            "expires_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class JobSerializer(serializers.ModelSerializer):
    steps = JobStepSerializer(many=True, read_only=True)
    workflow_run = WorkflowRunSerializer(read_only=True)
    callbacks = CallbackSerializer(many=True, read_only=True)
    owner_email = serializers.EmailField(source="owner.email", read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    conversation_title = serializers.CharField(source="conversation.title", read_only=True)

    class Meta:
        model = Job
        fields = [
            "id",
            "owner",
            "owner_email",
            "organization",
            "organization_name",
            "conversation",
            "conversation_title",
            "request_id",
            "idempotency_key",
            "task_type",
            "status",
            "input_payload",
            "queue_name",
            "celery_task_id",
            "progress_percent",
            "retry_count",
            "max_retries",
            "last_retry_at",
            "cancel_requested_at",
            "entitlement_snapshot",
            "reserved_credits",
            "actual_credits",
            "result",
            "error_code",
            "error_message",
            "trace_id",
            "n8n_workflow_id",
            "n8n_execution_id",
            "callback_url",
            "deadline",
            "started_at",
            "completed_at",
            "created_at",
            "updated_at",
            "steps",
            "workflow_run",
            "callbacks",
        ]
        read_only_fields = fields


class JobCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Job
        fields = ["idempotency_key", "task_type", "input_payload", "callback_url", "deadline"]
        extra_kwargs = {
            "idempotency_key": {"required": True},
            "task_type": {"required": True},
            "input_payload": {"required": True},
        }

    def validate_task_type(self, value):
        from apps.jobs.dispatch import NATIVE_TASK_TYPES

        # Only accept work a worker can execute; unsupported types would reserve
        # credits and then fail with UNSUPPORTED_TASK_TYPE.
        supported = sorted(NATIVE_TASK_TYPES)
        if value not in supported:
            raise serializers.ValidationError(f"Unsupported task_type. Must be one of: {supported}")
        return value

    def validate_callback_url(self, value):
        return validate_callback_url(value) if value else value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs.get("task_type") != Job.TaskType.RAG_QUERY:
            return attrs
        payload = attrs.get("input_payload")
        if not isinstance(payload, dict):
            raise serializers.ValidationError({"input_payload": "RAG input_payload must be an object."})
        query = payload.get("query")
        collection_ids = payload.get("collection_ids")
        if not isinstance(query, str) or not query.strip() or len(query) > 10_000:
            raise serializers.ValidationError(
                {"input_payload": "RAG jobs require a query between 1 and 10000 characters."}
            )
        if not isinstance(collection_ids, list) or not collection_ids or len(collection_ids) > 100:
            raise serializers.ValidationError(
                {"input_payload": "RAG jobs require between 1 and 100 collection_ids."}
            )
        request = self.context["request"]
        from apps.identity.authorization import organization_for_request
        from apps.knowledge.models import Collection

        organization = organization_for_request(request, required=True)
        unique_ids = list(dict.fromkeys(str(value) for value in collection_ids))
        matched = Collection.objects.filter(
            id__in=unique_ids, organization=organization, is_active=True
        ).count()
        if matched != len(unique_ids):
            raise serializers.ValidationError(
                {"input_payload": "One or more collections are unavailable in the selected organization."}
            )
        payload["collection_ids"] = unique_ids
        try:
            payload["top_k"] = min(max(int(payload.get("top_k", 5)), 1), 100)
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError(
                {"input_payload": "top_k must be an integer between 1 and 100."}
            ) from exc
        attrs["input_payload"] = payload
        return attrs


class JobStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Job.Status.choices)
    result = serializers.JSONField(required=False)
    error_code = serializers.CharField(required=False, allow_blank=True)
    error_message = serializers.CharField(required=False, allow_blank=True)
    n8n_execution_id = serializers.CharField(required=False, allow_blank=True)
    progress_percent = serializers.IntegerField(required=False, min_value=0, max_value=100)
    steps_completed = serializers.IntegerField(required=False, min_value=0)
    total_steps = serializers.IntegerField(required=False, min_value=0)
    actual_credits = serializers.DecimalField(required=False, min_value=0, max_digits=20, decimal_places=6)
