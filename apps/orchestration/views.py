"""n8n -> Django endpoints (signed) and the orchestration admin/tenant APIs.

Every ``/n8n/...`` endpoint authenticates the raw body with the timestamped,
nonce-bound HMAC of :mod:`apps.core.signing` (``N8N_WEBHOOK_SECRET``; the
error relay uses ``N8N_SENTRY_RELAY_SECRET``), rejects replays, and records
each accepted nonce durably (:class:`WorkflowCallback`) so a callback is
processed at most once even if the replay cache is lost. Callbacks are bound to
Django state: a run callback must name the run's current attempt, a document
push must belong to an open sync delivery.
"""

from __future__ import annotations

import logging
from typing import Any

import sentry_sdk
from django.conf import settings
from django.db import IntegrityError, transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.metrics import WORKFLOW_CALLBACKS
from apps.core.signing import NONCE_HEADER
from apps.core.tracing import span_from_headers
from apps.core.views import APIView
from apps.core.webhooks import n8n_callback_secrets, reject_unsigned
from apps.events.outbox import enqueue_outbox_event
from apps.identity.authorization import HasOrganizationWriteAccess, organization_for_request
from apps.orchestration import deliveries, knowledge, runs
from apps.orchestration.models import Automation, WorkflowCallback, WorkflowDefinition, WorkflowEventDelivery
from apps.orchestration.serializers import (
    AutomationSerializer,
    DeliveryStatusCallbackSerializer,
    DocumentsCallbackSerializer,
    ErrorRelaySerializer,
    RunEventsCallbackSerializer,
    RunStatusCallbackSerializer,
    WorkflowDefinitionSerializer,
    WorkflowDeliverySerializer,
    WorkflowEventSerializer,
)

logger = logging.getLogger(__name__)


class _SignedN8nView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []
    throttle_classes: list[Any] = []
    kind = "callback"
    source = "n8n"

    def secrets(self) -> list[str]:
        return n8n_callback_secrets()

    def authenticate_n8n(self, request: Request, target_id: str = "") -> Response | None:
        rejection = reject_unsigned(request, source=self.source, secrets_=self.secrets())
        if rejection is not None:
            WORKFLOW_CALLBACKS.labels(self.kind, "rejected").inc()
            return rejection
        try:
            with transaction.atomic():
                WorkflowCallback.objects.create(
                    nonce=request.headers[NONCE_HEADER], kind=self.kind, target_id=target_id[:64]
                )
        except IntegrityError:
            WORKFLOW_CALLBACKS.labels(self.kind, "duplicate").inc()
            return Response({"duplicate": True}, status=status.HTTP_200_OK)
        return None

    def finish(self, http_status: int, body: dict[str, Any]) -> Response:
        WORKFLOW_CALLBACKS.labels(self.kind, "accepted" if http_status < 400 else f"http_{http_status}").inc()
        return Response(body, status=http_status)


@extend_schema(request=RunStatusCallbackSerializer, responses={200: OpenApiTypes.OBJECT}, auth=[])
class RunStatusCallbackView(_SignedN8nView):
    kind = "run_status"

    def post(self, request: Request, run_id: Any) -> Response:
        if (rejection := self.authenticate_n8n(request, str(run_id))) is not None:
            return rejection
        serializer = RunStatusCallbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with span_from_headers("n8n run callback", request.headers, run_id=str(run_id)):
            return self.finish(*runs.apply_run_callback(run_id, serializer.normalized()))


@extend_schema(responses={200: OpenApiTypes.OBJECT}, auth=[])
class RunStateView(_SignedN8nView):
    """Long workflows poll this (signed GET, empty body) to honour cancellation."""

    kind = "run_state"

    def get(self, request: Request, run_id: Any) -> Response:
        rejection = reject_unsigned(request, source=self.source, secrets_=self.secrets())
        if rejection is not None:
            return rejection
        state = runs.run_state(run_id)
        if state is None:
            return Response({"detail": "Workflow run not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(state)


@extend_schema(request=RunEventsCallbackSerializer, responses={200: OpenApiTypes.OBJECT}, auth=[])
class RunEventsCallbackView(_SignedN8nView):
    kind = "run_events"

    def post(self, request: Request, run_id: Any) -> Response:
        from apps.jobs.models import WorkflowRun

        if (rejection := self.authenticate_n8n(request, str(run_id))) is not None:
            return rejection
        serializer = RunEventsCallbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            run = (
                WorkflowRun.objects.select_for_update(of=("self",))
                .select_related("job")
                .filter(id=run_id)
                .first()
            )
            if run is None:
                return self.finish(404, {"detail": "Workflow run not found."})
            if serializer.validated_data["attempt"] != run.attempt:
                return self.finish(
                    409, {"detail": "Callback is for a superseded attempt.", "code": "stale_attempt"}
                )
            for item in serializer.validated_data["events"]:
                if item["type"] == "step.completed":
                    run.steps_completed += 1
                runs._event(
                    run, "step", step_type=item["type"], step=item.get("name", ""), data=item.get("data")
                )
            run.save(update_fields=["steps_completed", "updated_at"])
        return self.finish(200, {"accepted": len(serializer.validated_data["events"])})


@extend_schema(request=DeliveryStatusCallbackSerializer, responses={200: OpenApiTypes.OBJECT}, auth=[])
class DeliveryStatusCallbackView(_SignedN8nView):
    kind = "delivery_status"

    def post(self, request: Request, delivery_id: Any) -> Response:
        if (rejection := self.authenticate_n8n(request, str(delivery_id))) is not None:
            return rejection
        serializer = DeliveryStatusCallbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.finish(*deliveries.apply_delivery_callback(delivery_id, serializer.normalized()))


@extend_schema(request=DocumentsCallbackSerializer, responses={200: OpenApiTypes.OBJECT}, auth=[])
class KnowledgeDocumentsCallbackView(_SignedN8nView):
    kind = "knowledge_documents"

    def post(self, request: Request, source_id: Any) -> Response:
        if (rejection := self.authenticate_n8n(request, str(source_id))) is not None:
            return rejection
        serializer = DocumentsCallbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.finish(*knowledge.ingest_documents(source_id, serializer.validated_data))


@extend_schema(request=WorkflowEventSerializer, responses={202: OpenApiTypes.OBJECT}, auth=[])
class WorkflowEventsView(_SignedN8nView):
    """Workflow-defined events, published to Kafka through the outbox as ``orchestration.n8n.<name>``."""

    kind = "workflow_event"

    def post(self, request: Request) -> Response:
        if (rejection := self.authenticate_n8n(request)) is not None:
            return rejection
        serializer = WorkflowEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if len(str(data["data"])) > 64 * 1024:
            return self.finish(400, {"detail": "data must be at most 64 KB."})
        with transaction.atomic():
            event = enqueue_outbox_event(
                topic=f"orchestration.n8n.{data['name']}",
                event_key=data["key"],
                payload={
                    "data": data["data"],
                    "run_id": str(data["runId"]) if data.get("runId") else None,
                    "delivery_id": str(data["deliveryId"]) if data.get("deliveryId") else None,
                },
            )
        return self.finish(202, {"eventId": str(event.id), "topic": event.topic})


@extend_schema(request=ErrorRelaySerializer, responses={202: None}, auth=[])
class ErrorRelayView(_SignedN8nView):
    """The n8n error workflow: Sentry event, audit trail, Kafka event and a failed attempt (retry)."""

    kind = "error_relay"
    source = "n8n_relay"

    def secrets(self) -> list[str]:
        return [settings.N8N_SENTRY_RELAY_SECRET]

    def post(self, request: Request) -> Response:
        if (rejection := self.authenticate_n8n(request)) is not None:
            return rejection
        serializer = ErrorRelaySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = {k: v for k, v in serializer.validated_data.items() if v not in (None, "")}
        message = str(data.get("message") or "n8n workflow failure")[:1000]
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("source", "n8n")
            scope.set_tag("n8n.workflow", str(data.get("workflowName") or data.get("workflowId") or ""))
            scope.set_context("n8n", data)
            sentry_sdk.capture_message(message, level="error")
        outcome = None
        if execution_id := data.get("executionId"):
            outcome = runs.fail_by_execution(execution_id, message) or deliveries.fail_by_execution(
                execution_id, message
            )
        with transaction.atomic():
            enqueue_outbox_event(
                topic="orchestration.workflow.error",
                event_key=str(data.get("workflowId") or "n8n"),
                payload={**data, "message": message, "retry_outcome": outcome},
            )
        logger.error("n8n reported workflow failure", extra={"workflow_id": data.get("workflowId")})
        return self.finish(202, {"accepted": True, "retry": outcome})


# Staff and tenant APIs --------------------------------------------------------


class WorkflowDefinitionViewSet(viewsets.ReadOnlyModelViewSet):
    """Registered workflow versions and their n8n sync state (staff only)."""

    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = WorkflowDefinitionSerializer
    queryset = WorkflowDefinition.objects.all()


class WorkflowDeliveryViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsAdminUser]
    serializer_class = WorkflowDeliverySerializer
    queryset = WorkflowEventDelivery.objects.select_related("definition")


class AutomationViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """Tenant automations run by the ``scheduled-automation`` n8n workflow."""

    serializer_class = AutomationSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_permissions(self) -> list[Any]:
        if self.action in {"list", "retrieve"}:
            return [IsAuthenticated()]
        return [IsAuthenticated(), HasOrganizationWriteAccess()]

    def get_queryset(self) -> Any:
        if getattr(self, "swagger_fake_view", False):
            return Automation.objects.none()
        organization = organization_for_request(self.request)
        if organization is None:
            return Automation.objects.none()
        return Automation.objects.filter(organization=organization)

    def get_serializer_context(self) -> dict[str, Any]:
        context = super().get_serializer_context()
        if not getattr(self, "swagger_fake_view", False) and self.request.user.is_authenticated:
            context["organization"] = organization_for_request(self.request, required=True)
        return context

    def perform_create(self, serializer: Any) -> None:
        from apps.orchestration.registry import active_definition

        if active_definition("scheduled-automation") is None:
            raise PermissionDenied(
                "Automations are unavailable: no scheduled-automation workflow is registered."
            )
        serializer.save(
            organization=organization_for_request(self.request, required=True), created_by=self.request.user
        )

    @extend_schema(request=None, responses={201: OpenApiTypes.OBJECT})
    @action(detail=True, methods=["post"])
    def run(self, request: Request, pk: Any = None) -> Response:
        from apps.orchestration.automations import start_automation_run

        automation = self.get_object()
        if automation.created_by_id != request.user.id:
            automation.created_by = request.user  # the caller owns (and is billed for) a manual run
        job = start_automation_run(automation, manual=True)
        return Response({"jobId": str(job.id), "status": job.status}, status=status.HTTP_201_CREATED)
