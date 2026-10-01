from rest_framework.routers import DefaultRouter

from apps.tools.views import (
    McpServerViewSet,
    TenantToolPolicyViewSet,
    ToolApprovalViewSet,
    ToolCredentialViewSet,
    ToolInvocationViewSet,
    ToolViewSet,
)

router = DefaultRouter()
router.register("tools", ToolViewSet, basename="tool")
router.register("tool-policies", TenantToolPolicyViewSet, basename="tool-policy")
router.register("tool-credentials", ToolCredentialViewSet, basename="tool-credential")
router.register("tool-approvals", ToolApprovalViewSet, basename="tool-approval")
router.register("tool-invocations", ToolInvocationViewSet, basename="tool-invocation")
router.register("mcp/servers", McpServerViewSet, basename="mcp-server")
urlpatterns = router.urls
