from __future__ import annotations

import asyncio
import json

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.http import StreamingHttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.agents.engine import create_run, request_cancel
from apps.agents.models import AgentDefinition, AgentRun, AgentStep
from apps.agents.serializers import (
    AgentDefinitionSerializer,
    AgentRunCreateSerializer,
    AgentRunSerializer,
    AgentStepSerializer,
)
from apps.agents.tasks import dispatch_run
from apps.core.pagination import CreatedCursorPagination, UpdatedCursorPagination
from apps.core.throttling import AgentRunThrottle, BurstThrottle
from apps.identity.authorization import (
    HasOrganizationWriteAccess,
    organization_for_request,
    require_organization_write_access,
    tenant_scoped_queryset,
    user_has_role,
)
from apps.identity.models import Role

ACTIVE_STATUSES = (AgentRun.Status.QUEUED, AgentRun.Status.RUNNING, AgentRun.Status.WAITING_APPROVAL)


class AgentConcurrencyLimit(APIException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = "Too many agent runs are active for this organization; retry later."
    default_code = "agent_concurrency_limit"


def _idempotency_key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key or len(key) > 255:
        raise ValidationError({"idempotencyKey": ["A 1-255 character Idempotency-Key header is required."]})
    return key


def start_run(request: Request, *, organization, agent: AgentDefinition | None = None) -> Response:
    serializer = AgentRunCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    require_organization_write_access(request.user, organization.id)
    key = _idempotency_key(request)
    with transaction.atomic():
        replay = AgentRun.objects.filter(
            organization=organization, user=request.user, idempotency_key=key
        ).first()
        if replay is not None:
            response = Response(AgentRunSerializer(replay).data, status=status.HTTP_200_OK)
            response["Idempotency-Replayed"] = "true"
            return response
        from apps.usage import services as metering
        from apps.usage.concurrency import enforce_concurrency
        from apps.usage.models import Feature

        # Locks the tenant row so concurrent submissions cannot both pass the limit.
        enforce_concurrency(organization, "agent_runs", error=AgentConcurrencyLimit)
        run, _created = create_run(
            user=request.user,
            organization=organization,
            input_text=serializer.validated_data["input"],
            agent=agent,
            graph=serializer.validated_data.get("graph"),
            requested_tools=serializer.validated_data.get("tools"),
            idempotency_key=key,
            trace_id=getattr(request, "trace_id", ""),
        )
        metering.reserve(
            organization=organization,
            user=request.user,
            feature=Feature.AGENT_RUNS,
            source_type="agent_run",
            source_id=run.id,
        )
        dispatch_run(run)
    return Response(AgentRunSerializer(run).data, status=status.HTTP_202_ACCEPTED)


class AgentDefinitionViewSet(viewsets.ModelViewSet):
    """Tenant agent definitions (masterplan §8.5)."""

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = AgentDefinitionSerializer
    pagination_class = UpdatedCursorPagination
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return tenant_scoped_queryset(AgentDefinition.objects.all(), self.request.user).order_by(
            "-updated_at", "-id"
        )

    def perform_create(self, serializer):
        serializer.save(
            organization=organization_for_request(self.request, required=True), created_by=self.request.user
        )

    @extend_schema(
        request=AgentRunCreateSerializer, responses={202: AgentRunSerializer, 200: AgentRunSerializer}
    )
    @action(detail=True, methods=["post"], throttle_classes=[AgentRunThrottle, BurstThrottle])
    def runs(self, request: Request, pk=None) -> Response:
        agent = self.get_object()
        if not agent.is_active:
            raise NotFound("This agent is inactive.")
        return start_run(request, organization=agent.organization, agent=agent)


class AgentRunViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Agent runs: users see their own; organization admins see all tenant runs."""

    permission_classes = [IsAuthenticated]
    serializer_class = AgentRunSerializer
    pagination_class = CreatedCursorPagination

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # OpenAPI generation has no user
            return AgentRun.objects.none()
        queryset = tenant_scoped_queryset(AgentRun.objects.all(), self.request.user).select_related(
            "evaluation"
        )
        admin_org_ids = [
            org_id
            for org_id in self.request.user.organizations.values_list("id", flat=True)
            if user_has_role(self.request.user, Role.RoleType.ADMIN, org_id)
        ]
        from django.db.models import Q

        queryset = queryset.filter(Q(user=self.request.user) | Q(organization_id__in=admin_org_ids))
        if run_status := self.request.query_params.get("status"):
            queryset = queryset.filter(status=run_status)
        return queryset.order_by("-created_at", "-id")

    def get_throttles(self):
        if self.action == "create":
            return [AgentRunThrottle(), BurstThrottle()]
        return super().get_throttles()

    @extend_schema(
        request=AgentRunCreateSerializer, responses={202: AgentRunSerializer, 200: AgentRunSerializer}
    )
    def create(self, request: Request, *args, **kwargs) -> Response:
        return start_run(request, organization=organization_for_request(request, required=True))

    @extend_schema(request=None, responses={200: AgentRunSerializer})
    @action(detail=True, methods=["post"])
    def cancel(self, request: Request, pk=None) -> Response:
        run = self.get_object()
        require_organization_write_access(request.user, run.organization_id)
        return Response(AgentRunSerializer(request_cancel(run)).data)

    @extend_schema(responses={200: AgentStepSerializer(many=True)})
    @action(detail=True, methods=["get"])
    def steps(self, request: Request, pk=None) -> Response:
        run = self.get_object()
        return Response(AgentStepSerializer(run.trace.order_by("sequence"), many=True).data)

    @extend_schema(responses={(200, "text/event-stream"): OpenApiTypes.STR})
    @action(detail=True, methods=["get"])
    def events(self, request: Request, pk=None) -> StreamingHttpResponse:
        """SSE stream of trace steps (``step``) then the final run (``completed``/``failed``/…)."""
        run = self.get_object()
        run_id = run.id
        try:
            after = int(request.headers.get("Last-Event-ID", "0") or 0)
        except ValueError:
            after = 0

        def poll(last_sequence: int) -> tuple[list[tuple[int, str]], str, str | None]:
            """Load new steps and the run state; all ORM/serializer work stays synchronous."""
            try:
                if not connection.in_atomic_block:
                    close_old_connections()
                steps = AgentStep.objects.filter(run_id=run_id, sequence__gt=last_sequence).order_by(
                    "sequence"
                )
                events = [
                    (step.sequence, json.dumps(AgentStepSerializer(step).data, default=str)) for step in steps
                ]
                current = AgentRun.objects.select_related("evaluation").get(id=run_id)
                final = None
                if current.is_terminal or current.status == AgentRun.Status.WAITING_APPROVAL:
                    final = json.dumps(AgentRunSerializer(current).data, default=str)
                return events, current.status, final
            finally:
                if not connection.in_atomic_block:
                    close_old_connections()

        async def stream():
            loop = asyncio.get_running_loop()
            deadline = loop.time() + max(1, settings.CHAT_SSE_MAX_SECONDS)
            last_sequence = after
            last_heartbeat = loop.time()
            while loop.time() < deadline:
                events, run_status, final = await sync_to_async(poll, thread_sensitive=True)(last_sequence)
                for sequence, payload in events:
                    last_sequence = sequence
                    yield f"id: {sequence}\nevent: step\ndata: {payload}\n\n"
                if final is not None:
                    yield f"event: {run_status}\ndata: {final}\n\n"
                    return
                if loop.time() - last_heartbeat >= settings.CHAT_SSE_HEARTBEAT_SECONDS:
                    yield "event: heartbeat\ndata: {}\n\n"
                    last_heartbeat = loop.time()
                await asyncio.sleep(max(0.1, settings.CHAT_SSE_POLL_SECONDS))
            yield 'event: timeout\ndata: {"code":"stream_timeout"}\n\n'

        response = StreamingHttpResponse(stream(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache, no-store"
        response["X-Accel-Buffering"] = "no"
        return response
