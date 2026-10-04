from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.orchestration.views import (
    AutomationViewSet,
    DeliveryStatusCallbackView,
    ErrorRelayView,
    KnowledgeDocumentsCallbackView,
    RunEventsCallbackView,
    RunStateView,
    RunStatusCallbackView,
    WorkflowDefinitionViewSet,
    WorkflowDeliveryViewSet,
    WorkflowEventsView,
)

router = DefaultRouter()
router.register("automations", AutomationViewSet, basename="automation")
router.register("n8n/workflows", WorkflowDefinitionViewSet, basename="n8n-workflow")
router.register("n8n/deliveries", WorkflowDeliveryViewSet, basename="n8n-delivery")

urlpatterns = [
    # Signed n8n -> Django callbacks.
    path("n8n/runs/<uuid:run_id>/", RunStateView.as_view(), name="n8n-run-state"),
    path("n8n/runs/<uuid:run_id>/status/", RunStatusCallbackView.as_view(), name="n8n-run-status"),
    path("n8n/runs/<uuid:run_id>/events/", RunEventsCallbackView.as_view(), name="n8n-run-events"),
    path(
        "n8n/deliveries/<uuid:delivery_id>/status/",
        DeliveryStatusCallbackView.as_view(),
        name="n8n-delivery-status",
    ),
    path(
        "n8n/knowledge/sources/<uuid:source_id>/documents/",
        KnowledgeDocumentsCallbackView.as_view(),
        name="n8n-knowledge-documents",
    ),
    path("n8n/events/", WorkflowEventsView.as_view(), name="n8n-workflow-events"),
    path("n8n/errors/", ErrorRelayView.as_view(), name="n8n-error-relay"),
    path("", include(router.urls)),
]
