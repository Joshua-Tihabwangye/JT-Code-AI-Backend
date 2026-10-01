"""Tool governance: tenant policies, encrypted credentials, MCP servers, approvals and audit."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class TenantToolPolicy(models.Model):
    """Per-tenant enablement and configuration of one tool (absent = tool default)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="tool_policies"
    )
    tool_name = models.CharField(max_length=150)
    enabled = models.BooleanField(default=False)
    # Adapter-specific restrictions, e.g. {"allowed_domains": [...]} for web tools.
    config = models.JSONField(default=dict, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("organization", "tool_name"), name="uniq_tool_policy_org_tool")
        ]

    def __str__(self) -> str:
        return f"{self.tool_name} ({'on' if self.enabled else 'off'})"


class ToolCredential(models.Model):
    """A tenant-scoped, encrypted-at-rest integration credential. The secret is write-only."""

    class Provider(models.TextChoices):
        GITHUB = "github", "GitHub App installation"
        SLACK = "slack", "Slack bot"
        HTTP = "http", "External HTTP API"
        MCP = "mcp", "MCP server"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="tool_credentials"
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    name = models.SlugField(max_length=64)
    encrypted_secret = models.TextField(blank=True)
    # Non-secret scoping (installation id, allowed repos/channels, base URL, paths, methods).
    metadata = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    rotated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "provider", "name"), name="uniq_tool_credential_org_provider_name"
            )
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.name}"


class McpServer(models.Model):
    """A registered remote MCP server; discovered tools stay disabled until allowlisted."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DISABLED = "disabled", "Disabled"
        ERROR = "error", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="mcp_servers"
    )
    slug = models.SlugField(max_length=40)
    name = models.CharField(max_length=120)
    url = models.URLField(max_length=500)
    credential = models.ForeignKey(
        ToolCredential, on_delete=models.SET_NULL, related_name="mcp_servers", null=True, blank=True
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    discovered_tools = models.JSONField(default=list, blank=True)
    allowed_tools = models.JSONField(default=list, blank=True)
    # Allowlisted tools the admin has confirmed are read-only (no approval needed).
    read_only_tools = models.JSONField(default=list, blank=True)
    last_discovered_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("organization", "slug"), name="uniq_mcp_server_org_slug")
        ]

    def __str__(self) -> str:
        return f"MCP {self.slug} ({self.status})"


class ToolApproval(models.Model):
    """Human approval for one side-effecting call, bound to its exact arguments."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"
        EXECUTED = "executed", "Executed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="tool_approvals"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="requested_tool_approvals"
    )
    agent_run = models.ForeignKey(
        "agents.AgentRun", on_delete=models.CASCADE, related_name="tool_approvals", null=True, blank=True
    )
    tool_call_id = models.CharField(max_length=100, blank=True)
    tool_name = models.CharField(max_length=150)
    arguments = models.JSONField(default=dict)
    arguments_digest = models.CharField(max_length=64)
    summary = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="decided_tool_approvals",
        null=True,
        blank=True,
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    expires_at = models.DateTimeField()
    executed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("organization", "status", "-created_at"))]
        constraints = [
            models.UniqueConstraint(
                fields=("agent_run", "tool_call_id"),
                condition=~models.Q(tool_call_id=""),
                name="uniq_tool_approval_run_call",
            )
        ]

    def __str__(self) -> str:
        return f"{self.tool_name} approval ({self.status})"


class ToolInvocation(models.Model):
    """Audit record for every tool call attempt, allowed or not."""

    class Status(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        DENIED = "denied", "Denied"
        PENDING_APPROVAL = "pending_approval", "Pending approval"
        REJECTED = "rejected", "Rejected by approver"

    class Source(models.TextChoices):
        AGENT = "agent", "Agent run"
        API = "api", "Direct API"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "identity.Organization", on_delete=models.CASCADE, related_name="tool_invocations"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="tool_invocations", null=True
    )
    agent_run = models.ForeignKey(
        "agents.AgentRun", on_delete=models.SET_NULL, related_name="tool_invocations", null=True, blank=True
    )
    tool_call_id = models.CharField(max_length=100, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices)
    tool_name = models.CharField(max_length=150)
    side_effect = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices)
    deny_code = models.CharField(max_length=60, blank=True)
    arguments_redacted = models.JSONField(default=dict)
    arguments_digest = models.CharField(max_length=64)
    # Stored (capped) only so an interrupted agent run can resume without re-executing.
    output = models.TextField(blank=True)
    output_digest = models.CharField(max_length=64, blank=True)
    output_chars = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    approval = models.ForeignKey(
        ToolApproval, on_delete=models.SET_NULL, related_name="invocations", null=True, blank=True
    )
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    trace_id = models.CharField(max_length=100, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("organization", "-created_at")),
            models.Index(fields=("agent_run", "tool_call_id")),
            models.Index(fields=("status", "-created_at")),
        ]

    def __str__(self) -> str:
        return f"{self.tool_name} {self.status}"
