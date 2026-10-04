from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.integrations.views import (
    APIKeyViewSet,
    ConnectorAccountViewSet,
    ConnectorViewSet,
    IncomingWebhookView,
    IntegrationViewSet,
    KafkaConsumerViewSet,
    WebhookDeliveryViewSet,
    WebhookViewSet,
)

router = DefaultRouter()
router.register(r"integrations", IntegrationViewSet, basename="integration")
router.register(r"connectors", ConnectorViewSet, basename="connector")
router.register(r"connector-accounts", ConnectorAccountViewSet, basename="connector-account")
router.register(r"webhooks", WebhookViewSet, basename="webhook")
router.register(r"webhook-deliveries", WebhookDeliveryViewSet, basename="webhook-delivery")
router.register(r"api-keys", APIKeyViewSet, basename="api-key")
router.register(r"kafka-consumers", KafkaConsumerViewSet, basename="kafka-consumer")

urlpatterns = [
    # Public, signature-authenticated receiver. It must not share the router's
    # ``webhooks/<id>/`` path, whose authenticated detail route would shadow it.
    path("inbound-webhooks/<uuid:webhook_id>/", IncomingWebhookView.as_view(), name="incoming-webhook"),
    path("", include(router.urls)),
]
