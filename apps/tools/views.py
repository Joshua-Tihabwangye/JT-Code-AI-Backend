"""Tool, MCP, credential, approval and audit APIs (masterplan §8.9)."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.pagination import CreatedCursorPagination
from apps.core.throttling import BurstThrottle
from apps.identity.authorization import organization_for_request, user_has_role
from apps.identity.models import Role
from apps.tools.approvals import decide
from apps.tools.gateway import ToolDenied, execute_tool, is_enabled, tenant_policy
from apps.tools.models import McpServer, TenantToolPolicy, ToolApproval, ToolCredential, ToolInvocation
from apps.tools.registry import specs_for_organization
from apps.tools.serializers import (
    ApprovalDecisionSerializer,
    McpServerSerializer,
    TenantToolPolicySerializer,
    ToolApprovalSerializer,
    ToolCredentialSerializer,
    ToolExecuteSerializer,
    ToolInvocationSerializer,
)


class IsOrganizationAdmin(BasePermission):
    """Tool governance (policies, credentials, MCP servers, audit) is admin-only."""

    def has_permission(self, request: Request, view: Any) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False
        organization = organization_for_request(request, required=True)
        return user_has_role(request.user, Role.RoleType.ADMIN, organization.id)


class _TenantAdminViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsOrganizationAdmin]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    model: Any = None

    def organization(self):
        return organization_for_request(self.request, required=True)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return self.model.objects.none()
        return self.model.objects.filter(organization=self.organization()).order_by("-created_at")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if not getattr(self, "swagger_fake_view", False):
            context["organization"] = self.organization()
        return context

    def perform_create(self, serializer):
        try:
            serializer.save(organization=self.organization(), **self.creator_fields())
        except IntegrityError:
            raise ValidationError({"detail": "An entry with this name already exists."}) from None

    def creator_fields(self) -> dict[str, Any]:
        return {}


class TenantToolPolicyViewSet(_TenantAdminViewSet):
    serializer_class = TenantToolPolicySerializer
    model = TenantToolPolicy

    def creator_fields(self) -> dict[str, Any]:
        return {"updated_by": self.request.user}

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)


class ToolCredentialViewSet(_TenantAdminViewSet):
    serializer_class = ToolCredentialSerializer
    model = ToolCredential

    def creator_fields(self) -> dict[str, Any]:
        return {"created_by": self.request.user}


class McpServerViewSet(_TenantAdminViewSet):
    serializer_class = McpServerSerializer
    model = McpServer

    def creator_fields(self) -> dict[str, Any]:
        return {"created_by": self.request.user}

    @extend_schema(request=None, responses={200: McpServerSerializer})
    @action(detail=True, methods=["post"])
    def discover(self, request: Request, pk=None) -> Response:
        from apps.tools.adapters.mcp import discover

        return Response(
            McpServerSerializer(discover(self.get_object()), context=self.get_serializer_context()).data
        )


class ToolInvocationViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Audit log of every tool call in the tenant (admin-only)."""

    permission_classes = [IsAuthenticated, IsOrganizationAdmin]
    serializer_class = ToolInvocationSerializer
    pagination_class = CreatedCursorPagination

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ToolInvocation.objects.none()
        queryset = ToolInvocation.objects.filter(
            organization=organization_for_request(self.request, required=True)
        )
        if tool := self.request.query_params.get("tool"):
            queryset = queryset.filter(tool_name=tool)
        if invocation_status := self.request.query_params.get("status"):
            queryset = queryset.filter(status=invocation_status)
        return queryset.order_by("-created_at", "-id")


class ToolApprovalViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Pending and decided approvals in the tenant; editors and admins decide."""

    permission_classes = [IsAuthenticated]
    serializer_class = ToolApprovalSerializer
    pagination_class = CreatedCursorPagination

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ToolApproval.objects.none()
        queryset = ToolApproval.objects.filter(
            organization=organization_for_request(self.request, required=True)
        )
        if approval_status := self.request.query_params.get("status"):
            queryset = queryset.filter(status=approval_status)
        return queryset.order_by("-created_at", "-id")

    def _decide(self, request: Request, approve: bool) -> Response:
        approval = self.get_object()
        serializer = ApprovalDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            approval, result = decide(
                approval, user=request.user, approve=approve, note=serializer.validated_data.get("note", "")
            )
        except ToolDenied as denial:
            code = status.HTTP_403_FORBIDDEN if denial.code == "FORBIDDEN_ROLE" else status.HTTP_409_CONFLICT
            return Response({"code": denial.code.lower(), "message": str(denial)}, status=code)
        body: dict[str, Any] = {"approval": ToolApprovalSerializer(approval).data}
        if result is not None:
            body["result"] = {
                "status": result.status,
                "output": result.content,
                "invocationId": str(result.invocation.id),
            }
        return Response(body)

    @extend_schema(request=ApprovalDecisionSerializer, responses={200: ToolApprovalSerializer})
    @action(detail=True, methods=["post"])
    def approve(self, request: Request, pk=None) -> Response:
        return self._decide(request, approve=True)

    @extend_schema(request=ApprovalDecisionSerializer, responses={200: ToolApprovalSerializer})
    @action(detail=True, methods=["post"])
    def reject(self, request: Request, pk=None) -> Response:
        return self._decide(request, approve=False)


class ToolViewSet(viewsets.ViewSet):
    """Tools available to the caller, and controlled direct execution."""

    permission_classes = [IsAuthenticated]
    lookup_value_regex = r"[A-Za-z0-9_.-]+"  # unknown names reach the gateway (UNKNOWN_TOOL)

    @extend_schema(responses={200: dict})
    def list(self, request: Request) -> Response:
        from apps.tools.gateway import _role_rank
        from apps.tools.registry import ROLE_RANK

        organization = organization_for_request(request, required=True)
        rank = _role_rank(request.user, organization.id)
        tools = []
        for name, spec in sorted(specs_for_organization(organization.id).items()):
            if not is_enabled(spec, organization.id, tenant_policy(organization.id, name)):
                continue
            conditional = callable(spec.side_effect)
            tools.append(
                {
                    "name": name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                    "sideEffect": "conditional" if conditional else bool(spec.side_effect),
                    "requiresApproval": "conditional" if conditional else bool(spec.side_effect),
                    "minRole": spec.min_role,
                    "available": rank >= ROLE_RANK[spec.min_role],
                }
            )
        return Response({"results": tools})

    @extend_schema(request=ToolExecuteSerializer, responses={200: dict, 202: dict, 403: dict})
    @action(detail=True, methods=["post"], throttle_classes=[BurstThrottle])
    def execute(self, request: Request, pk: str | None = None) -> Response:
        serializer = ToolExecuteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization = organization_for_request(request, required=True)
        if not request.user.organizations.filter(id=organization.id).exists():
            raise PermissionDenied("Not a member of this organization.")
        result = execute_tool(
            pk or "",
            serializer.validated_data.get("arguments") or {},
            user=request.user,
            organization_id=organization.id,
            source=ToolInvocation.Source.API,
            approval_id=serializer.validated_data.get("approvalId"),
            trace_id=getattr(request, "trace_id", ""),
        )
        body: dict[str, Any] = {"status": result.status, "invocationId": str(result.invocation.id)}
        if result.status == ToolInvocation.Status.PENDING_APPROVAL and result.approval is not None:
            body["approvalId"] = str(result.approval.id)
            return Response(body, status=status.HTTP_202_ACCEPTED)
        if result.status == ToolInvocation.Status.DENIED:
            body["code"] = result.invocation.deny_code.lower()
            body["message"] = result.content
            return Response(body, status=status.HTTP_403_FORBIDDEN)
        body["output"] = result.content
        if result.status == ToolInvocation.Status.FAILED:
            return Response(body, status=status.HTTP_502_BAD_GATEWAY)
        return Response(body)
