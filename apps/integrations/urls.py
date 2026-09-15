from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.integrations.views import (
    ConnectorViewSet,
    ConnectorAccountViewSet,
    WebhookViewSet,
    WebhookDeliveryViewSet,
    IncomingWebhookView,
    APIKeyViewSet,
    KafkaConsumerViewSet,
)

router = DefaultRouter()
router.register(r'connectors', ConnectorViewSet, basename='connector')
router.register(r'connector-accounts', ConnectorAccountViewSet, basename='connector-account')
router.register(r'webhooks', WebhookViewSet, basename='webhook')
router.register(r'webhook-deliveries', WebhookDeliveryViewSet, basename='webhook-delivery')
router.register(r'api-keys', APIKeyViewSet, basename='api-key')
router.register(r'kafka-consumers', KafkaConsumerViewSet, basename='kafka-consumer')

urlpatterns = [
    path('', include(router.urls)),
    path('webhooks/<uuid:webhook_id>/', IncomingWebhookView.as_view(), name='incoming-webhook'),
]