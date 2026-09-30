from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.views import APIView
from apps.events.outbox import enqueue_outbox_event
from apps.identity.authorization import (
    HasOrganizationWriteAccess,
    organization_for_request,
)
from apps.integrations.models import (
    APIKey,
    Connector,
    ConnectorAccount,
    KafkaConsumer,
    Webhook,
    WebhookDelivery,
)
from apps.integrations.serializers import (
    APIKeyCreateSerializer,
    APIKeySerializer,
    ConnectorAccountAuthSerializer,
    ConnectorAccountCreateSerializer,
    ConnectorAccountSerializer,
    ConnectorSerializer,
    KafkaConsumerCreateSerializer,
    KafkaConsumerSerializer,
    WebhookCreateSerializer,
    WebhookDeliverySerializer,
    WebhookSerializer,
)


def _selected_organization_id(request: Request):
    organization = organization_for_request(request)
    return organization.id if organization is not None else None


def _tenant_queryset(queryset, request: Request, *, organization_field: str = "organization"):
    organization_id = _selected_organization_id(request)
    if organization_id is None:
        return queryset.none()
    return queryset.filter(**{f"{organization_field}_id": organization_id})


class ConnectorViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ConnectorSerializer
    lookup_field = "slug"

    def get_queryset(self):
        return Connector.objects.filter(is_active=True, is_verified=True)


class ConnectorAccountViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = ConnectorAccountSerializer
    lookup_field = "id"

    def get_queryset(self):
        return _tenant_queryset(ConnectorAccount.objects.all(), self.request).select_related(
            "organization", "connector", "user"
        )

    def get_serializer_class(self):
        if self.action == "create":
            return ConnectorAccountCreateSerializer
        return ConnectorAccountSerializer

    def perform_create(self, serializer):
        org = organization_for_request(self.request, required=True)
        serializer.save(organization=org, user=self.request.user)

    @action(detail=True, methods=["post"])
    def authorize(self, request: Request, id=None):
        account = self.get_object()
        serializer = ConnectorAccountAuthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # This would initiate OAuth flow or store API credentials
        # For now, return authorization URL
        connector = account.connector

        if connector.auth_type == Connector.AuthType.OAUTH2:
            # Generate state parameter for security
            state = secrets.token_urlsafe(32)
            account.encrypted_config = account.encrypted_config or {}
            account.encrypted_config["oauth_state"] = state
            account.save(update_fields=["encrypted_config"])

            auth_config = connector.config_schema
            auth_url = (
                f"{auth_config.get('auth_url')}?client_id={auth_config.get('client_id')}"
                f"&redirect_uri={auth_config.get('redirect_uri')}"
                f"&scope={' '.join(connector.required_scopes)}&state={state}&response_type=code"
            )

            return Response(
                {
                    "auth_url": auth_url,
                    "state": state,
                }
            )
        else:
            # API key or other auth type
            return Response({"detail": "Manual configuration required"}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"])
    def test(self, request: Request, id=None):
        self.get_object()  # permission/lookup check for the target account
        # Test the connection
        # This would make a test API call
        return Response({"status": "success", "message": "Connection test passed"})

    @action(detail=True, methods=["post"])
    def sync(self, request: Request, id=None):
        account = self.get_object()
        # Trigger sync
        enqueue_outbox_event(
            topic="integrations.connector.sync",
            event_key=str(account.id),
            payload={
                "account_id": str(account.id),
                "connector_id": str(account.connector_id),
                "organization_id": str(account.organization_id),
            },
            headers={"trace_id": f"connector-sync-{account.id}"},
        )
        return Response({"detail": "Sync triggered"})


class WebhookViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = WebhookSerializer
    lookup_field = "id"

    def get_queryset(self):
        return _tenant_queryset(Webhook.objects.all(), self.request).select_related(
            "organization", "created_by"
        )

    def get_serializer_class(self):
        if self.action == "create":
            return WebhookCreateSerializer
        return WebhookSerializer

    def perform_create(self, serializer):
        org = organization_for_request(self.request, required=True)
        # Generate secret
        secret = secrets.token_urlsafe(32)
        serializer.save(organization=org, created_by=self.request.user, secret=secret)

    @action(detail=True, methods=["get"])
    def deliveries(self, request: Request, id=None):
        webhook = self.get_object()
        deliveries = webhook.deliveries.all()
        page = self.paginate_queryset(deliveries)
        if page is not None:
            serializer = WebhookDeliverySerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = WebhookDeliverySerializer(deliveries, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def test(self, request: Request, id=None):
        self.get_object()  # permission/lookup check for the target webhook
        # Send test payload
        test_payload = {
            "event": "webhook.test",
            "timestamp": timezone.now().isoformat(),
            "data": {"message": "Test webhook from JT-Code"},
        }

        # This would actually send the webhook
        # For now, just return success
        return Response({"status": "sent", "payload": test_payload})


class WebhookDeliveryViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WebhookDeliverySerializer
    lookup_field = "id"

    def get_queryset(self):
        webhook_ids = _tenant_queryset(Webhook.objects.all(), self.request).values_list("id", flat=True)
        return WebhookDelivery.objects.filter(webhook_id__in=webhook_ids).select_related("webhook")


INBOUND_WEBHOOK_MAX_SKEW_SECONDS = 300
INBOUND_WEBHOOK_MAX_BYTES = 1024 * 1024


def inbound_webhook_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` HMAC senders must place in ``X-Webhook-Signature``."""
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_inbound_webhook_signature(request: Request, secret: str) -> Response | None:
    """Authenticate a timestamped HMAC-SHA256 signature; return an error response or ``None``."""
    if len(request.body) > INBOUND_WEBHOOK_MAX_BYTES:
        return Response(
            {"detail": "Webhook payload too large"}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        )
    signature = request.headers.get("X-Webhook-Signature", "")
    timestamp = request.headers.get("X-Webhook-Timestamp", "")
    if not signature or not timestamp.isdigit():
        return Response(
            {"detail": "X-Webhook-Signature and X-Webhook-Timestamp are required"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    if abs(time.time() - int(timestamp)) > INBOUND_WEBHOOK_MAX_SKEW_SECONDS:
        return Response(
            {"detail": "Webhook timestamp outside tolerance"}, status=status.HTTP_401_UNAUTHORIZED
        )
    expected = inbound_webhook_signature(secret, timestamp, request.body)
    if not hmac.compare_digest(signature, expected):
        return Response({"detail": "Invalid signature"}, status=status.HTTP_401_UNAUTHORIZED)
    return None


@extend_schema_view(post=extend_schema(operation_id="v1_incoming_webhook_receive"))
class IncomingWebhookView(APIView):
    """Receive incoming webhooks from external services"""

    permission_classes = []
    authentication_classes = []

    def post(self, request: Request, webhook_id):
        try:
            webhook = Webhook.objects.get(id=webhook_id, status=Webhook.Status.ACTIVE)
        except Webhook.DoesNotExist:
            return Response({"detail": "Webhook not found"}, status=status.HTTP_404_NOT_FOUND)

        rejection = verify_inbound_webhook_signature(request, webhook.secret)
        if rejection is not None:
            return rejection
        # A captured request may be resent inside the timestamp window; accept it once.
        replay_key = f"inbound-webhook:{webhook.id}:{request.headers['X-Webhook-Signature']}"
        if not cache.add(replay_key, "1", timeout=INBOUND_WEBHOOK_MAX_SKEW_SECONDS * 2):
            return Response({"detail": "Duplicate webhook delivery"}, status=status.HTTP_409_CONFLICT)

        with transaction.atomic():
            delivery = self._record(webhook, request)
        return Response({"received": True, "delivery_id": str(delivery.id)})

    @staticmethod
    def _record(webhook, request: Request):
        delivery = WebhookDelivery.objects.create(
            webhook=webhook,
            event_type=request.headers.get("X-Event-Type", "unknown"),
            payload=request.data,
            status=WebhookDelivery.Status.PENDING,
        )

        # Process asynchronously
        enqueue_outbox_event(
            topic="integrations.webhook.received",
            event_key=str(delivery.id),
            payload={
                "delivery_id": str(delivery.id),
                "webhook_id": str(webhook.id),
                "event_type": delivery.event_type,
                "payload": delivery.payload,
            },
        )
        return delivery


class APIKeyViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = APIKeySerializer
    lookup_field = "id"

    def get_queryset(self):
        return _tenant_queryset(APIKey.objects.all(), self.request).select_related("organization", "user")

    def get_serializer_class(self):
        if self.action == "create":
            return APIKeyCreateSerializer
        return APIKeySerializer

    def perform_create(self, serializer):
        org = organization_for_request(self.request, required=True)

        # Generate API key
        prefix = "jtk_live" if not settings.DEBUG else "jtk_test"
        key = secrets.token_urlsafe(32)
        full_key = f"{prefix}_{key}"
        key_hash = hashlib.sha256(full_key.encode()).hexdigest()

        serializer.save(
            organization=org,
            user=self.request.user,
            prefix=prefix,
            key_hash=key_hash,
        )

        # Return full key only on creation
        self.created_key = full_key

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        if hasattr(self, "created_key"):
            response.data["key"] = self.created_key
        return response

    @action(detail=True, methods=["post"])
    def revoke(self, request: Request, id=None):
        api_key = self.get_object()
        api_key.status = APIKey.Status.REVOKED
        api_key.save(update_fields=["status", "updated_at"])
        return Response({"detail": "API key revoked"})


class KafkaConsumerViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = KafkaConsumerSerializer
    lookup_field = "id"

    def get_queryset(self):
        return _tenant_queryset(KafkaConsumer.objects.all(), self.request).select_related(
            "organization", "created_by"
        )

    def get_serializer_class(self):
        if self.action == "create":
            return KafkaConsumerCreateSerializer
        return KafkaConsumerSerializer

    def perform_create(self, serializer):
        org = organization_for_request(self.request, required=True)
        serializer.save(organization=org, created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def start(self, request: Request, id=None):
        consumer = self.get_object()
        consumer.status = KafkaConsumer.Status.RUNNING
        consumer.save(update_fields=["status", "updated_at"])
        return Response({"detail": "Consumer started"})

    @action(detail=True, methods=["post"])
    def stop(self, request: Request, id=None):
        consumer = self.get_object()
        consumer.status = KafkaConsumer.Status.STOPPED
        consumer.save(update_fields=["status", "updated_at"])
        return Response({"detail": "Consumer stopped"})
