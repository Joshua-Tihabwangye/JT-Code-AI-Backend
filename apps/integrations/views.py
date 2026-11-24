from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.signing import constant_time_equals
from apps.core.views import APIView
from apps.events.outbox import enqueue_outbox_event
from apps.identity.authorization import (
    HasOrganizationWriteAccess,
    organization_for_request,
    user_has_role,
)
from apps.identity.models import Role
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
        from apps.integrations.facade import PROVIDERS, apply_check

        account = self.get_object()
        if account.connector.slug not in PROVIDERS:
            return Response(
                {"status": "unsupported", "message": "This connector has no connection check."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ok, message = apply_check(account)
        return Response({"status": "success" if ok else "failed", "message": message})

    @action(detail=True, methods=["post"])
    def sync(self, request: Request, id=None):
        account = self.get_object()
        queued = sync_integration_sources(account)
        enqueue_outbox_event(
            topic="integrations.connector.sync",
            event_key=str(account.id),
            payload={
                "account_id": str(account.id),
                "connector_id": str(account.connector_id),
                "organization_id": str(account.organization_id),
                "queued_sources": queued,
            },
        )
        return Response({"detail": "Sync triggered", "queued_sources": queued})


class HasOrganizationAdminAccess(HasOrganizationWriteAccess):
    """Webhooks receive the organization's events, so only its admins manage them or read deliveries."""

    message = "Only organization admins can manage webhooks."

    def has_permission(self, request, view) -> bool:
        if not super().has_permission(request, view):
            return False
        organization = organization_for_request(request, required=True)
        return user_has_role(request.user, Role.RoleType.ADMIN, organization.id)


class WebhookViewSet(viewsets.ModelViewSet):
    """Outbound webhooks (``apps.integrations.outbound``). The signing secret is returned once, at
    creation and at rotation; deliveries are signed with it (``X-JT-Signature``)."""

    permission_classes = [IsAuthenticated, HasOrganizationAdminAccess]
    serializer_class = WebhookSerializer
    lookup_field = "id"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return _tenant_queryset(Webhook.objects.all(), self.request).select_related("created_by")

    def get_serializer_class(self):
        if self.action == "create":
            return WebhookCreateSerializer
        return WebhookSerializer

    def create(self, request, *args, **kwargs):
        serializer = WebhookCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        secret = f"whsec_{secrets.token_urlsafe(32)}"
        webhook = serializer.save(
            organization=organization_for_request(request, required=True),
            created_by=request.user,
            secret=secret,
        )
        data = WebhookSerializer(webhook, context=self.get_serializer_context()).data
        return Response({**data, "secret": secret}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="rotate-secret")
    def rotate_secret(self, request: Request, id=None):
        webhook = self.get_object()
        webhook.secret = f"whsec_{secrets.token_urlsafe(32)}"
        webhook.save(update_fields=["secret", "updated_at"])
        data = WebhookSerializer(webhook, context=self.get_serializer_context()).data
        return Response({**data, "secret": webhook.secret})

    @action(detail=True, methods=["get"])
    def deliveries(self, request: Request, id=None):
        webhook = self.get_object()
        deliveries = webhook.deliveries.all()
        page = self.paginate_queryset(deliveries)
        if page is not None:
            return self.get_paginated_response(WebhookDeliverySerializer(page, many=True).data)
        return Response(WebhookDeliverySerializer(deliveries, many=True).data)

    @action(detail=True, methods=["post"])
    def test(self, request: Request, id=None):
        """Send a signed ``webhook.test`` event now (one attempt); poll ``deliveries`` for the result."""
        from apps.integrations.outbound import create_test_delivery

        delivery = create_test_delivery(self.get_object())
        return Response(WebhookDeliverySerializer(delivery).data, status=status.HTTP_202_ACCEPTED)


class WebhookDeliveryViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationAdminAccess]
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
    if not constant_time_equals(signature, expected):
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
    """Organization API keys (``X-API-Key``, see ``apps.integrations.authentication``).

    Admins see and revoke every key of the organization; other members only their own. The key is
    returned once, in the create response; only its SHA-256 digest is stored.
    """

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = APIKeySerializer
    lookup_field = "id"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        queryset = _tenant_queryset(APIKey.objects.all(), self.request).select_related("organization", "user")
        organization = organization_for_request(self.request, required=True)
        if not user_has_role(self.request.user, Role.RoleType.ADMIN, organization.id):
            queryset = queryset.filter(user=self.request.user)
        return queryset

    def get_serializer_class(self):
        if self.action == "create":
            return APIKeyCreateSerializer
        return APIKeySerializer

    def create(self, request, *args, **kwargs):
        from apps.integrations.authentication import key_digest

        serializer = APIKeyCreateSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        organization = organization_for_request(request, required=True)
        prefix = "jtk_test" if settings.DEBUG else "jtk_live"
        full_key = f"{prefix}_{secrets.token_urlsafe(32)}"
        api_key = serializer.save(
            organization=organization, user=request.user, prefix=prefix, key_hash=key_digest(full_key)
        )
        data = APIKeySerializer(api_key, context=self.get_serializer_context()).data
        # The only time the key is ever returned.
        return Response({**data, "key": full_key}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def revoke(self, request: Request, id=None):
        api_key = self.get_object()
        if api_key.status == APIKey.Status.ACTIVE:
            api_key.status = APIKey.Status.REVOKED
            api_key.save(update_fields=["status", "updated_at"])
        return Response(APIKeySerializer(api_key, context=self.get_serializer_context()).data)


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


class IntegrationViewSet(viewsets.ViewSet):
    """The frontend's ``/integrations/`` contract (GitHub, Google Drive, Notion, Slack).

    Connections are verified by the n8n ``integration-test`` workflow; syncing
    runs the ``knowledge-integration-sync`` workflow for the integration's
    knowledge sources. See ``apps.integrations.facade``.
    """

    lookup_field = "id"
    lookup_value_regex = "[0-9a-f-]{36}"
    queryset = ConnectorAccount.objects.none()  # schema generation only

    def get_permissions(self):
        if self.action in {"list", "retrieve", "test"}:
            return [IsAuthenticated()]
        return [IsAuthenticated(), HasOrganizationWriteAccess()]

    def _accounts(self):
        from apps.integrations.facade import PROVIDERS

        return (
            _tenant_queryset(ConnectorAccount.objects.all(), self.request)
            .filter(connector__slug__in=list(PROVIDERS))
            .select_related("connector")
        )

    def _get(self, id):
        from django.shortcuts import get_object_or_404

        account = get_object_or_404(self._accounts(), id=id)
        # A plain ViewSet has no get_object(), so run the object-level role check here.
        self.check_object_permissions(self.request, account)
        return account

    @extend_schema(operation_id="v1_integrations_list", responses={200: OpenApiTypes.OBJECT})
    def list(self, request: Request):
        from apps.integrations.facade import serialize

        return Response([serialize(account) for account in self._accounts().order_by("created_at")])

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def retrieve(self, request: Request, id=None):
        from apps.integrations.facade import serialize

        return Response(serialize(self._get(id)))

    @extend_schema(request=OpenApiTypes.OBJECT, responses={201: OpenApiTypes.OBJECT})
    @action(detail=False, methods=["post"])
    def connect(self, request: Request):
        from apps.integrations.facade import PROVIDERS, apply_check, connector_for, serialize, validate_config

        key = str(request.data.get("key") or "")
        config = validate_config(key, request.data.get("config") or {})
        display_name = str(request.data.get("displayName") or PROVIDERS[key]["name"])[:255]
        organization = organization_for_request(request, required=True)
        connector = connector_for(key)
        account = ConnectorAccount.objects.filter(organization=organization, connector=connector).first()
        if account is None:
            account = ConnectorAccount(organization=organization, connector=connector, user=request.user)
        account.name = display_name
        account.metadata = {**(account.metadata or {}), "config": config}
        account.status = ConnectorAccount.Status.PENDING
        account.save()
        apply_check(account)
        return Response(serialize(account), status=status.HTTP_201_CREATED)

    @extend_schema(request=OpenApiTypes.OBJECT, responses={200: OpenApiTypes.OBJECT})
    def partial_update(self, request: Request, id=None):
        from apps.integrations.facade import serialize, validate_config

        account = self._get(id)
        if "config" in request.data:
            account.metadata = {
                **(account.metadata or {}),
                "config": validate_config(account.connector.slug, request.data.get("config") or {}),
            }
        if "displayName" in request.data:
            account.name = str(request.data.get("displayName") or account.name)[:255]
        account.save(update_fields=["metadata", "name", "updated_at"])
        return Response(serialize(account))

    @extend_schema(responses={204: None})
    def destroy(self, request: Request, id=None):
        account = self._get(id)
        account.status = ConnectorAccount.Status.REVOKED
        account.last_sync_at = None
        account.save(update_fields=["status", "last_sync_at", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=None, responses={200: OpenApiTypes.OBJECT})
    @action(detail=True, methods=["post"])
    def test(self, request: Request, id=None):
        from apps.integrations.facade import apply_check

        ok, message = apply_check(self._get(id))
        return Response({"ok": ok, "message": message})

    @extend_schema(request=None, responses={200: OpenApiTypes.OBJECT})
    @action(detail=True, methods=["post"])
    def reconnect(self, request: Request, id=None):
        from apps.integrations.facade import apply_check, serialize

        account = self._get(id)
        apply_check(account)
        return Response(serialize(account))

    @extend_schema(request=None, responses={202: OpenApiTypes.OBJECT})
    @action(detail=True, methods=["post"])
    def sync(self, request: Request, id=None):
        account = self._get(id)
        queued = sync_integration_sources(account)
        return Response({"queuedSources": queued}, status=status.HTTP_202_ACCEPTED)


def sync_integration_sources(account: ConnectorAccount) -> int:
    """Queue a sync of every active knowledge source that reads from ``account``."""
    from apps.knowledge.models import Source
    from apps.knowledge.tasks import sync_source

    if account.status != ConnectorAccount.Status.ACTIVE:
        raise ValidationError({"detail": "Connect the integration before syncing it."})
    sources = Source.objects.filter(
        source_type=Source.SourceType.INTEGRATION,
        is_active=True,
        collection__organization_id=account.organization_id,
        config__integrationId=str(account.id),
    ).values_list("id", flat=True)
    for source_id in sources:
        transaction.on_commit(lambda source_id=source_id: sync_source.delay(str(source_id)))
    return len(sources)
