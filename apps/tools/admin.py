from django.contrib import admin

from apps.tools.models import McpServer, TenantToolPolicy, ToolApproval, ToolCredential, ToolInvocation


@admin.register(TenantToolPolicy)
class TenantToolPolicyAdmin(admin.ModelAdmin):
    list_display = ("tool_name", "organization", "enabled", "updated_at")
    list_filter = ("enabled",)
    search_fields = ("tool_name", "organization__name")


@admin.register(ToolCredential)
class ToolCredentialAdmin(admin.ModelAdmin):
    """The encrypted secret is never displayed or editable here; rotate it through the API."""

    list_display = ("provider", "name", "organization", "is_active", "rotated_at")
    list_filter = ("provider", "is_active")
    exclude = ("encrypted_secret",)


@admin.register(McpServer)
class McpServerAdmin(admin.ModelAdmin):
    list_display = ("slug", "organization", "status", "last_discovered_at")
    list_filter = ("status",)


@admin.register(ToolApproval)
class ToolApprovalAdmin(admin.ModelAdmin):
    list_display = ("tool_name", "organization", "status", "requested_by", "decided_by", "created_at")
    list_filter = ("status", "tool_name")
    readonly_fields = [field.name for field in ToolApproval._meta.fields]


@admin.register(ToolInvocation)
class ToolInvocationAdmin(admin.ModelAdmin):
    list_display = ("tool_name", "organization", "source", "status", "deny_code", "created_at")
    list_filter = ("status", "source", "side_effect")
    search_fields = ("tool_name", "trace_id")
    readonly_fields = [field.name for field in ToolInvocation._meta.fields]
