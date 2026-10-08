from __future__ import annotations

from typing import Any

from django.utils import timezone
from rest_framework import serializers

from apps.orchestration.models import Automation, WorkflowDefinition, WorkflowEventDelivery


class CallbackErrorSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=64, required=False, allow_blank=True)
    message = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    retryable = serializers.BooleanField(required=False, default=True)


class RunStatusCallbackSerializer(serializers.Serializer):
    attempt = serializers.IntegerField(min_value=1)
    status = serializers.ChoiceField(choices=("running", "waiting_approval", "completed", "failed"))
    progress = serializers.IntegerField(min_value=0, max_value=100, required=False)
    result = serializers.JSONField(required=False)
    error = CallbackErrorSerializer(required=False)
    executionId = serializers.CharField(max_length=100, required=False, allow_blank=True)
    stepsCompleted = serializers.IntegerField(min_value=0, required=False)
    totalSteps = serializers.IntegerField(min_value=0, required=False)
    actualCredits = serializers.DecimalField(max_digits=20, decimal_places=6, min_value=0, required=False)

    def normalized(self) -> dict[str, Any]:
        data = dict(self.validated_data)
        renames = {
            "progress": "progress_percent",
            "executionId": "execution_id",
            "stepsCompleted": "steps_completed",
            "totalSteps": "total_steps",
            "actualCredits": "actual_credits",
        }
        return {renames.get(key, key): value for key, value in data.items()}


class RunEventSerializer(serializers.Serializer):
    type = serializers.RegexField(r"^[a-z][a-z0-9_.]{0,60}$")
    name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    data = serializers.JSONField(required=False)


class RunEventsCallbackSerializer(serializers.Serializer):
    attempt = serializers.IntegerField(min_value=1)
    events = RunEventSerializer(many=True, max_length=50)


class DeliveryStatusCallbackSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=("completed", "failed"))
    error = CallbackErrorSerializer(required=False)
    executionId = serializers.CharField(max_length=64, required=False, allow_blank=True)
    summary = serializers.JSONField(required=False)

    def normalized(self) -> dict[str, Any]:
        data = dict(self.validated_data)
        if "executionId" in data:
            data["execution_id"] = data.pop("executionId")
        return data


class DocumentsCallbackSerializer(serializers.Serializer):
    deliveryId = serializers.UUIDField()
    syncRunId = serializers.UUIDField(required=False, allow_null=True)
    documents = serializers.ListField(child=serializers.DictField(), max_length=100)
    final = serializers.BooleanField(default=False)


class WorkflowEventSerializer(serializers.Serializer):
    """A custom workflow event n8n publishes to Kafka as ``orchestration.n8n.<name>``."""

    name = serializers.RegexField(r"^[a-z][a-z0-9_]{0,39}$")
    key = serializers.CharField(max_length=200)
    data = serializers.DictField()
    runId = serializers.UUIDField(required=False)
    deliveryId = serializers.UUIDField(required=False)


class ErrorRelaySerializer(serializers.Serializer):
    message = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    workflowId = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    workflowName = serializers.CharField(max_length=200, required=False, allow_blank=True, allow_null=True)
    executionId = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    step = serializers.CharField(max_length=200, required=False, allow_blank=True, allow_null=True)
    mode = serializers.CharField(max_length=40, required=False, allow_blank=True, allow_null=True)
    errorCode = serializers.CharField(max_length=64, required=False, allow_blank=True, allow_null=True)


class WorkflowDefinitionSerializer(serializers.ModelSerializer):
    taskTypes = serializers.JSONField(source="task_types", read_only=True)
    eventTypes = serializers.JSONField(source="event_types", read_only=True)
    webhookPath = serializers.CharField(source="webhook_path", read_only=True)
    isActive = serializers.BooleanField(source="is_active", read_only=True)
    n8nWorkflowId = serializers.CharField(source="n8n_workflow_id", read_only=True)
    n8nActive = serializers.BooleanField(source="n8n_active", read_only=True)
    inSync = serializers.SerializerMethodField()
    syncedAt = serializers.DateTimeField(source="synced_at", read_only=True)
    syncError = serializers.CharField(source="sync_error", read_only=True)

    class Meta:
        model = WorkflowDefinition
        fields = [
            "id",
            "key",
            "version",
            "name",
            "kind",
            "description",
            "taskTypes",
            "eventTypes",
            "webhookPath",
            "isActive",
            "n8nWorkflowId",
            "n8nActive",
            "inSync",
            "syncedAt",
            "syncError",
        ]

    def get_inSync(self, obj: WorkflowDefinition) -> bool:
        return bool(obj.n8n_workflow_id) and obj.synced_checksum == obj.checksum


class WorkflowDeliverySerializer(serializers.ModelSerializer):
    workflow = serializers.CharField(source="definition.key", read_only=True)
    eventType = serializers.CharField(source="event_type", read_only=True)
    executionId = serializers.CharField(source="n8n_execution_id", read_only=True)
    lastError = serializers.CharField(source="last_error", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    completedAt = serializers.DateTimeField(source="completed_at", read_only=True)

    class Meta:
        model = WorkflowEventDelivery
        fields = [
            "id",
            "workflow",
            "eventType",
            "status",
            "attempts",
            "executionId",
            "lastError",
            "createdAt",
            "completedAt",
        ]


class AutomationSerializer(serializers.ModelSerializer):
    isActive = serializers.BooleanField(source="is_active", required=False)
    nextRunAt = serializers.DateTimeField(source="next_run_at", read_only=True)
    lastRunAt = serializers.DateTimeField(source="last_run_at", read_only=True)
    lastJobId = serializers.UUIDField(source="last_job_id", read_only=True)
    lastError = serializers.CharField(source="last_error", read_only=True)
    runCount = serializers.IntegerField(source="run_count", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = Automation
        fields = [
            "id",
            "name",
            "description",
            "schedule",
            "input",
            "isActive",
            "nextRunAt",
            "lastRunAt",
            "lastJobId",
            "lastError",
            "runCount",
            "createdAt",
        ]

    def validate_schedule(self, value: str) -> str:
        from apps.orchestration.automations import validate_schedule

        return validate_schedule(value)

    def validate_input(self, value: Any) -> dict[str, Any]:
        from apps.orchestration.automations import validate_input

        return validate_input(value, self.context["organization"])

    def _schedule_next(self, instance: Automation) -> None:
        from apps.orchestration.automations import next_run_after

        instance.next_run_at = (
            next_run_after(instance.schedule, timezone.now()) if instance.is_active else None
        )
        instance.save(update_fields=["next_run_at", "updated_at"])

    def create(self, validated_data: dict[str, Any]) -> Automation:
        instance = super().create(validated_data)
        self._schedule_next(instance)
        return instance

    def update(self, instance: Automation, validated_data: dict[str, Any]) -> Automation:
        instance = super().update(instance, validated_data)
        self._schedule_next(instance)
        return instance
