from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.conf import settings
from rest_framework import serializers

from apps.agents.graphs import GRAPHS, tool_catalog
from apps.agents.models import AgentDefinition, AgentEvaluation, AgentRun, AgentStep

MAX_INPUT_CHARS = 20_000


class AgentDefinitionSerializer(serializers.ModelSerializer):
    modelAlias = serializers.SlugField(source="model_alias", required=False, allow_blank=True)
    systemPrompt = serializers.CharField(source="system_prompt", required=False, allow_blank=True)
    allowedTools = serializers.ListField(
        source="allowed_tools", child=serializers.CharField(), required=False
    )
    maxSteps = serializers.IntegerField(source="max_steps", required=False, min_value=1)
    maxModelCalls = serializers.IntegerField(source="max_model_calls", required=False, min_value=1)
    maxToolCalls = serializers.IntegerField(source="max_tool_calls", required=False, min_value=0)
    maxCostUsd = serializers.DecimalField(
        source="max_cost_usd", max_digits=10, decimal_places=4, required=False, min_value=Decimal("0.0001")
    )
    maxDurationSeconds = serializers.IntegerField(source="max_duration_seconds", required=False, min_value=5)
    isActive = serializers.BooleanField(source="is_active", required=False)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = AgentDefinition
        fields = (
            "id",
            "name",
            "slug",
            "description",
            "graph",
            "systemPrompt",
            "modelAlias",
            "allowedTools",
            "maxSteps",
            "maxModelCalls",
            "maxToolCalls",
            "maxCostUsd",
            "maxDurationSeconds",
            "isActive",
            "createdAt",
            "updatedAt",
        )
        read_only_fields = ("id", "createdAt", "updatedAt")

    def validate_graph(self, value: str) -> str:
        if value not in GRAPHS:
            raise serializers.ValidationError(f"Unknown graph. Choose one of {sorted(GRAPHS)}.")
        return value

    def validate_modelAlias(self, value: str) -> str:  # noqa: N802 - DRF field hook name
        from apps.ai_gateway.models import ModelAlias

        if value and not ModelAlias.objects.filter(slug=value, is_active=True).exists():
            raise serializers.ValidationError("Unknown or inactive model alias.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        graph = attrs.get("graph") or getattr(self.instance, "graph", "")
        tools = attrs.get("allowed_tools")
        if tools is not None and graph in GRAPHS:
            request = self.context.get("request")
            organization_id = None
            if request is not None:
                from apps.identity.authorization import organization_for_request

                organization = organization_for_request(request)
                organization_id = organization.id if organization else None
            catalog = set(tool_catalog(organization_id))
            defaults = GRAPHS[graph].default_tools
            permitted = catalog if defaults == ("*",) else set(defaults) & catalog
            if unknown := sorted(set(tools) - permitted):
                raise serializers.ValidationError({"allowedTools": [f"Not permitted for {graph}: {unknown}"]})
        ceilings = {
            "max_steps": ("maxSteps", settings.LANGGRAPH_MAX_STEPS),
            "max_model_calls": ("maxModelCalls", settings.AGENT_MAX_ITERATIONS),
            "max_tool_calls": ("maxToolCalls", settings.AGENT_MAX_TOOL_CALLS),
            "max_cost_usd": ("maxCostUsd", Decimal(str(settings.AGENT_MAX_COST_USD))),
            "max_duration_seconds": ("maxDurationSeconds", settings.AGENT_MAX_DURATION_SECONDS),
        }
        errors = {
            field: [f"Must not exceed the platform limit of {ceiling}."]
            for name, (field, ceiling) in ceilings.items()
            if name in attrs and attrs[name] > ceiling
        }
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class AgentRunCreateSerializer(serializers.Serializer):
    input = serializers.CharField(max_length=MAX_INPUT_CHARS, trim_whitespace=True)
    graph = serializers.ChoiceField(choices=sorted(GRAPHS), required=False)
    tools = serializers.ListField(child=serializers.CharField(), required=False)


class AgentStepSerializer(serializers.ModelSerializer):
    modelRunId = serializers.UUIDField(source="model_run_id", read_only=True, allow_null=True)
    latencyMs = serializers.IntegerField(source="latency_ms", read_only=True, allow_null=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = AgentStep
        fields = (
            "sequence",
            "node",
            "kind",
            "outcome",
            "summary",
            "detail",
            "modelRunId",
            "latencyMs",
            "createdAt",
        )
        read_only_fields = fields


class AgentEvaluationSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentEvaluation
        fields = ("evaluator", "passed", "score", "metrics")
        read_only_fields = fields


class AgentRunSerializer(serializers.ModelSerializer):
    agentId = serializers.UUIDField(source="agent_id", read_only=True, allow_null=True)
    jobId = serializers.UUIDField(source="job_id", read_only=True, allow_null=True)
    intentSource = serializers.CharField(source="intent_source", read_only=True)
    inputText = serializers.CharField(source="input_text", read_only=True)
    finalOutput = serializers.CharField(source="final_output", read_only=True)
    modelAlias = serializers.CharField(source="model_alias", read_only=True)
    modelCalls = serializers.IntegerField(source="model_calls", read_only=True)
    toolCalls = serializers.IntegerField(source="tool_calls", read_only=True)
    inputTokens = serializers.IntegerField(source="input_tokens", read_only=True)
    outputTokens = serializers.IntegerField(source="output_tokens", read_only=True)
    costUsd = serializers.DecimalField(source="cost_usd", max_digits=12, decimal_places=8, read_only=True)
    errorCode = serializers.CharField(source="error_code", read_only=True)
    errorMessage = serializers.CharField(source="error_message", read_only=True)
    traceId = serializers.CharField(source="trace_id", read_only=True)
    cancelRequestedAt = serializers.DateTimeField(source="cancel_requested_at", read_only=True)
    startedAt = serializers.DateTimeField(source="started_at", read_only=True)
    completedAt = serializers.DateTimeField(source="completed_at", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    evaluation = serializers.SerializerMethodField()

    class Meta:
        model = AgentRun
        fields = (
            "id",
            "agentId",
            "jobId",
            "graph",
            "intent",
            "intentSource",
            "status",
            "inputText",
            "finalOutput",
            "modelAlias",
            "tools",
            "budget",
            "steps",
            "modelCalls",
            "toolCalls",
            "inputTokens",
            "outputTokens",
            "costUsd",
            "errorCode",
            "errorMessage",
            "traceId",
            "cancelRequestedAt",
            "startedAt",
            "completedAt",
            "createdAt",
            "evaluation",
        )
        read_only_fields = fields

    def get_evaluation(self, run: AgentRun) -> dict[str, Any] | None:
        evaluation = getattr(run, "evaluation", None)
        return AgentEvaluationSerializer(evaluation).data if evaluation else None
