from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.tools.crypto import encrypt_secret
from apps.tools.egress import EgressDenied, validate_url
from apps.tools.models import McpServer, TenantToolPolicy, ToolApproval, ToolCredential, ToolInvocation


class ToolExecuteSerializer(serializers.Serializer):
    arguments = serializers.DictField(required=False, default=dict)
    approvalId = serializers.UUIDField(required=False)


class TenantToolPolicySerializer(serializers.ModelSerializer):
    toolName = serializers.CharField(source="tool_name", max_length=150)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = TenantToolPolicy
        fields = ("id", "toolName", "enabled", "config", "updatedAt")
        read_only_fields = ("id", "updatedAt")

    def validate_toolName(self, value: str) -> str:  # noqa: N802 - DRF field hook
        from apps.tools.registry import static_specs

        if value not in static_specs():
            raise serializers.ValidationError(
                "Unknown tool (MCP tools are governed by their server allowlist)."
            )
        return value

    def validate_config(self, value: dict[str, Any]) -> dict[str, Any]:
        domains = value.get("allowed_domains")
        if domains is not None and (
            not isinstance(domains, list) or not all(isinstance(d, str) and d for d in domains)
        ):
            raise serializers.ValidationError({"allowed_domains": "Must be a list of host names."})
        return value


class ToolCredentialSerializer(serializers.ModelSerializer):
    """Secrets are write-only: they are encrypted on write and never returned."""

    secret = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=10000)
    hasSecret = serializers.SerializerMethodField()
    isActive = serializers.BooleanField(source="is_active", required=False)
    rotatedAt = serializers.DateTimeField(source="rotated_at", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = ToolCredential
        fields = (
            "id",
            "provider",
            "name",
            "metadata",
            "secret",
            "hasSecret",
            "isActive",
            "rotatedAt",
            "createdAt",
        )
        read_only_fields = ("id", "hasSecret", "rotatedAt", "createdAt")

    def get_hasSecret(self, credential: ToolCredential) -> bool:  # noqa: N802
        return bool(credential.encrypted_secret)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        provider = attrs.get("provider") or getattr(self.instance, "provider", "")
        metadata = attrs.get("metadata", getattr(self.instance, "metadata", {})) or {}
        if provider == ToolCredential.Provider.HTTP:
            try:
                validate_url(str(metadata.get("base_url", "")))
            except EgressDenied as exc:
                raise serializers.ValidationError({"metadata": f"base_url: {exc}"}) from None
        if (
            provider == ToolCredential.Provider.GITHUB
            and not str(metadata.get("installation_id", "")).isdigit()
        ):
            raise serializers.ValidationError({"metadata": "installation_id is required for GitHub."})
        return attrs

    def create(self, validated_data: dict[str, Any]) -> ToolCredential:
        secret = validated_data.pop("secret", "")
        return ToolCredential.objects.create(**validated_data, encrypted_secret=encrypt_secret(secret))

    def update(self, instance: ToolCredential, validated_data: dict[str, Any]) -> ToolCredential:
        from django.utils import timezone

        secret = validated_data.pop("secret", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if secret is not None:
            instance.encrypted_secret = encrypt_secret(secret)
            instance.rotated_at = timezone.now()
        instance.save()
        return instance


class McpServerSerializer(serializers.ModelSerializer):
    credentialId = serializers.PrimaryKeyRelatedField(
        source="credential", queryset=ToolCredential.objects.all(), required=False, allow_null=True
    )
    discoveredTools = serializers.JSONField(source="discovered_tools", read_only=True)
    allowedTools = serializers.ListField(
        source="allowed_tools", child=serializers.CharField(), required=False
    )
    readOnlyTools = serializers.ListField(
        source="read_only_tools", child=serializers.CharField(), required=False
    )
    lastDiscoveredAt = serializers.DateTimeField(source="last_discovered_at", read_only=True)
    lastError = serializers.CharField(source="last_error", read_only=True)

    class Meta:
        model = McpServer
        fields = (
            "id",
            "slug",
            "name",
            "url",
            "credentialId",
            "status",
            "discoveredTools",
            "allowedTools",
            "readOnlyTools",
            "lastDiscoveredAt",
            "lastError",
        )
        read_only_fields = ("id", "discoveredTools", "lastDiscoveredAt", "lastError")

    def validate_url(self, value: str) -> str:
        try:
            validate_url(value)
        except EgressDenied as exc:
            raise serializers.ValidationError(str(exc)) from None
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        organization = self.context["organization"]
        credential = attrs.get("credential")
        if credential is not None and credential.organization_id != organization.id:
            raise serializers.ValidationError({"credentialId": "Unknown credential."})
        discovered = {tool["name"] for tool in getattr(self.instance, "discovered_tools", []) or []}
        for field, label in (("allowed_tools", "allowedTools"), ("read_only_tools", "readOnlyTools")):
            if field in attrs and (unknown := sorted(set(attrs[field]) - discovered)):
                raise serializers.ValidationError({label: f"Not discovered on this server: {unknown}"})
        allowed = set(attrs.get("allowed_tools", getattr(self.instance, "allowed_tools", [])) or [])
        if "read_only_tools" in attrs and not set(attrs["read_only_tools"]) <= allowed:
            raise serializers.ValidationError({"readOnlyTools": "Read-only tools must also be allowlisted."})
        return attrs


class ToolApprovalSerializer(serializers.ModelSerializer):
    toolName = serializers.CharField(source="tool_name", read_only=True)
    agentRunId = serializers.UUIDField(source="agent_run_id", read_only=True, allow_null=True)
    requestedBy = serializers.UUIDField(source="requested_by_id", read_only=True)
    decidedBy = serializers.UUIDField(source="decided_by_id", read_only=True, allow_null=True)
    decidedAt = serializers.DateTimeField(source="decided_at", read_only=True)
    decisionNote = serializers.CharField(source="decision_note", read_only=True)
    expiresAt = serializers.DateTimeField(source="expires_at", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = ToolApproval
        fields = (
            "id",
            "toolName",
            "arguments",
            "summary",
            "status",
            "agentRunId",
            "requestedBy",
            "decidedBy",
            "decidedAt",
            "decisionNote",
            "expiresAt",
            "createdAt",
        )
        read_only_fields = fields


class ApprovalDecisionSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class ToolInvocationSerializer(serializers.ModelSerializer):
    toolName = serializers.CharField(source="tool_name", read_only=True)
    sideEffect = serializers.BooleanField(source="side_effect", read_only=True)
    denyCode = serializers.CharField(source="deny_code", read_only=True)
    arguments = serializers.JSONField(source="arguments_redacted", read_only=True)
    agentRunId = serializers.UUIDField(source="agent_run_id", read_only=True, allow_null=True)
    approvalId = serializers.UUIDField(source="approval_id", read_only=True, allow_null=True)
    outputChars = serializers.IntegerField(source="output_chars", read_only=True)
    latencyMs = serializers.IntegerField(source="latency_ms", read_only=True, allow_null=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = ToolInvocation
        fields = (
            "id",
            "toolName",
            "source",
            "sideEffect",
            "status",
            "denyCode",
            "arguments",
            "agentRunId",
            "approvalId",
            "outputChars",
            "latencyMs",
            "error",
            "createdAt",
        )
        read_only_fields = fields
