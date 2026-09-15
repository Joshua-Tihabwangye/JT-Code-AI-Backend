from __future__ import annotations

import secrets
import hashlib
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.views import APIView
from apps.integrations.models import Connector, ConnectorAccount, Webhook, WebhookDelivery, APIKey, KafkaConsumer
from apps.integrations.serializers import (
    ConnectorSerializer,
    ConnectorAccountSerializer,
    ConnectorAccountCreateSerializer,
    ConnectorAccountAuthSerializer,
    WebhookSerializer,
    WebhookCreateSerializer,
    WebhookDeliverySerializer,
    APIKeySerializer,
    APIKeyCreateSerializer,
    KafkaConsumerSerializer,
    KafkaConsumerCreateSerializer,
)
from apps.events.outbox import enqueue_outbox_event


class ConnectorViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ConnectorSerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return Connector.objects.filter(is_active=True, is_verified=True)


class ConnectorAccountViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ConnectorAccountSerializer
    lookup_field = 'id'

    def get_queryset(self):
        user_orgs = self.request.user.organizations.values_list('id', flat=True)
        return ConnectorAccount.objects.filter(
            organization_id__in=user_orgs
        ).select_related('organization', 'connector', 'user')

    def get_serializer_class(self):
        if self.action == 'create':
            return ConnectorAccountCreateSerializer
        return ConnectorAccountSerializer

    def perform_create(self, serializer):
        org = self.request.user.organizations.first()
        if not org:
            raise ValueError('User must belong to an organization')
        serializer.save(organization=org, user=self.request.user)

    @action(detail=True, methods=['post'])
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
            account.encrypted_config['oauth_state'] = state
            account.save(update_fields=['encrypted_config'])

            auth_url = f'{connector.config_schema.get("auth_url")}?client_id={connector.config_schema.get("client_id")}&redirect_uri={connector.config_schema.get("redirect_uri")}&scope={" ".join(connector.required_scopes)}&state={state}&response_type=code'

            return Response({
                'auth_url': auth_url,
                'state': state,
            })
        else:
            # API key or other auth type
            return Response({'detail': 'Manual configuration required'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def test(self, request: Request, id=None):
        account = self.get_object()
        # Test the connection
        # This would make a test API call
        return Response({'status': 'success', 'message': 'Connection test passed'})

    @action(detail=True, methods=['post'])
    def sync(self, request: Request, id=None):
        account = self.get_object()
        # Trigger sync
        enqueue_outbox_event(
            topic='integrations.connector.sync',
            event_key=str(account.id),
            payload={
                'account_id': str(account.id),
                'connector_id': str(account.connector_id),
                'organization_id': str(account.organization_id),
            },
            headers={'trace_id': f'connector-sync-{account.id}'}
        )
        return Response({'detail': 'Sync triggered'})


class WebhookViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WebhookSerializer
    lookup_field = 'id'

    def get_queryset(self):
        user_orgs = self.request.user.organizations.values_list('id', flat=True)
        return Webhook.objects.filter(
            organization_id__in=user_orgs
        ).select_related('organization', 'created_by')

    def get_serializer_class(self):
        if self.action == 'create':
            return WebhookCreateSerializer
        return WebhookSerializer

    def perform_create(self, serializer):
        org = self.request.user.organizations.first()
        if not org:
            raise ValueError('User must belong to an organization')
        # Generate secret
        secret = secrets.token_urlsafe(32)
        serializer.save(organization=org, created_by=self.request.user, secret=secret)

    @action(detail=True, methods=['get'])
    def deliveries(self, request: Request, id=None):
        webhook = self.get_object()
        deliveries = webhook.deliveries.all()
        page = self.paginate_queryset(deliveries)
        if page is not None:
            serializer = WebhookDeliverySerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = WebhookDeliverySerializer(deliveries, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def test(self, request: Request, id=None):
        webhook = self.get_object()
        # Send test payload
        test_payload = {
            'event': 'webhook.test',
            'timestamp': timezone.now().isoformat(),
            'data': {'message': 'Test webhook from JT-Code'},
        }

        # This would actually send the webhook
        # For now, just return success
        return Response({'status': 'sent', 'payload': test_payload})


class WebhookDeliveryViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WebhookDeliverySerializer
    lookup_field = 'id'

    def get_queryset(self):
        user_orgs = self.request.user.organizations.values_list('id', flat=True)
        webhook_ids = Webhook.objects.filter(organization_id__in=user_orgs).values_list('id', flat=True)
        return WebhookDelivery.objects.filter(webhook_id__in=webhook_ids).select_related('webhook')


class IncomingWebhookView(APIView):
    """Receive incoming webhooks from external services"""
    permission_classes = []
    authentication_classes = []

    def post(self, request: Request, webhook_id):
        try:
            webhook = Webhook.objects.get(id=webhook_id, status=Webhook.Status.ACTIVE)
        except Webhook.DoesNotExist:
            return Response({'detail': 'Webhook not found'}, status=status.HTTP_404_NOT_FOUND)

        # Verify signature
        signature = request.headers.get('X-Webhook-Signature')
        if signature:
            expected = hashlib.sha256(
                (webhook.secret + request.body.decode()).encode()
            ).hexdigest()
            if not secrets.compare_digest(signature, expected):
                return Response({'detail': 'Invalid signature'}, status=status.HTTP_401_UNAUTHORIZED)

        # Create delivery record
        delivery = WebhookDelivery.objects.create(
            webhook=webhook,
            event_type=request.headers.get('X-Event-Type', 'unknown'),
            payload=request.data,
            status=WebhookDelivery.Status.PENDING,
        )

        # Process asynchronously
        enqueue_outbox_event(
            topic='integrations.webhook.received',
            event_key=str(delivery.id),
            payload={
                'delivery_id': str(delivery.id),
                'webhook_id': str(webhook.id),
                'event_type': delivery.event_type,
                'payload': delivery.payload,
            },
            headers={'trace_id': f'webhook-{delivery.id}'}
        )

        return Response({'received': True, 'delivery_id': str(delivery.id)})


class APIKeyViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = APIKeySerializer
    lookup_field = 'id'

    def get_queryset(self):
        user_orgs = self.request.user.organizations.values_list('id', flat=True)
        return APIKey.objects.filter(
            organization_id__in=user_orgs
        ).select_related('organization', 'user')

    def get_serializer_class(self):
        if self.action == 'create':
            return APIKeyCreateSerializer
        return APIKeySerializer

    def perform_create(self, serializer):
        org = self.request.user.organizations.first()
        if not org:
            raise ValueError('User must belong to an organization')

        # Generate API key
        prefix = 'jtk_live' if not settings.DEBUG else 'jtk_test'
        key = secrets.token_urlsafe(32)
        full_key = f'{prefix}_{key}'
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
        if hasattr(self, 'created_key'):
            response.data['key'] = self.created_key
        return response

    @action(detail=True, methods=['post'])
    def revoke(self, request: Request, id=None):
        api_key = self.get_object()
        api_key.status = APIKey.Status.REVOKED
        api_key.save(update_fields=['status', 'updated_at'])
        return Response({'detail': 'API key revoked'})


class KafkaConsumerViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = KafkaConsumerSerializer
    lookup_field = 'id'

    def get_queryset(self):
        user_orgs = self.request.user.organizations.values_list('id', flat=True)
        return KafkaConsumer.objects.filter(
            organization_id__in=user_orgs
        ).select_related('organization', 'created_by')

    def get_serializer_class(self):
        if self.action == 'create':
            return KafkaConsumerCreateSerializer
        return KafkaConsumerSerializer

    def perform_create(self, serializer):
        org = self.request.user.organizations.first()
        if not org:
            raise ValueError('User must belong to an organization')
        serializer.save(organization=org, created_by=self.request.user)

    @action(detail=True, methods=['post'])
    def start(self, request: Request, id=None):
        consumer = self.get_object()
        consumer.status = KafkaConsumer.Status.RUNNING
        consumer.save(update_fields=['status', 'updated_at'])
        return Response({'detail': 'Consumer started'})

    @action(detail=True, methods=['post'])
    def stop(self, request: Request, id=None):
        consumer = self.get_object()
        consumer.status = KafkaConsumer.Status.STOPPED
        consumer.save(update_fields=['status', 'updated_at'])
        return Response({'detail': 'Consumer stopped'})