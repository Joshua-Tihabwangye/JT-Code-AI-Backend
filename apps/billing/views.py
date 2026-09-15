from __future__ import annotations

import stripe
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.views import APIView
from apps.billing.models import Plan, Subscription, CreditWallet, CreditLedger, Invoice, Payment
from apps.billing.serializers import (
    PlanSerializer,
    PlanListSerializer,
    SubscriptionSerializer,
    CreditWalletSerializer,
    CreditLedgerSerializer,
    InvoiceSerializer,
    PaymentSerializer,
    TopUpSerializer,
)
from apps.billing.services import CreditService, StripeService
from apps.events.outbox import enqueue_outbox_event


class PlanViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = PlanSerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return Plan.objects.filter(status=Plan.Status.ACTIVE).prefetch_related('entitlements')

    def get_serializer_class(self):
        if self.action == 'list':
            return PlanListSerializer
        return PlanSerializer

    @action(detail=True, methods=['post'])
    def subscribe(self, request: Request, slug=None):
        plan = self.get_object()
        user = request.user

        # Get or create organization
        org = user.organizations.first()
        if not org:
            return Response({'detail': 'User must belong to an organization'}, status=status.HTTP_400_BAD_REQUEST)

        # Check if already subscribed
        existing = Subscription.objects.filter(
            organization=org,
            status__in=[Subscription.Status.ACTIVE, Subscription.Status.TRIALING]
        ).first()
        if existing:
            return Response({'detail': 'Organization already has an active subscription'}, status=status.HTTP_400_BAD_REQUEST)

        # Create Stripe checkout session
        stripe_service = StripeService()
        checkout_url = stripe_service.create_checkout_session(
            organization=org,
            plan=plan,
            success_url=request.data.get('success_url'),
            cancel_url=request.data.get('cancel_url'),
        )

        return Response({'checkout_url': checkout_url})


class SubscriptionViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = SubscriptionSerializer
    lookup_field = 'id'

    def get_queryset(self):
        user_orgs = self.request.user.organizations.values_list('id', flat=True)
        return Subscription.objects.filter(organization_id__in=user_orgs).select_related('plan', 'organization')

    @action(detail=True, methods=['post'])
    def cancel(self, request: Request, id=None):
        subscription = self.get_object()
        if subscription.status not in [Subscription.Status.ACTIVE, Subscription.Status.TRIALING]:
            return Response({'detail': 'Subscription cannot be cancelled'}, status=status.HTTP_400_BAD_REQUEST)

        stripe_service = StripeService()
        stripe_service.cancel_subscription(subscription.provider_subscription_id)

        subscription.cancel_at_period_end = True
        subscription.save(update_fields=['cancel_at_period_end'])

        return Response(SubscriptionSerializer(subscription).data)

    @action(detail=True, methods=['post'])
    def reactivate(self, request: Request, id=None):
        subscription = self.get_object()
        if subscription.status != Subscription.Status.CANCELED or not subscription.cancel_at_period_end:
            return Response({'detail': 'Subscription cannot be reactivated'}, status=status.HTTP_400_BAD_REQUEST)

        stripe_service = StripeService()
        stripe_service.reactivate_subscription(subscription.provider_subscription_id)

        subscription.cancel_at_period_end = False
        subscription.save(update_fields=['cancel_at_period_end'])

        return Response(SubscriptionSerializer(subscription).data)


class CreditWalletViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = CreditWalletSerializer
    lookup_field = 'id'

    def get_queryset(self):
        user_orgs = self.request.user.organizations.values_list('id', flat=True)
        return CreditWallet.objects.filter(organization_id__in=user_orgs).select_related('organization')

    @action(detail=True, methods=['post'])
    def topup(self, request: Request, id=None):
        wallet = self.get_object()
        serializer = TopUpSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        amount_cents = serializer.validated_data['amount_cents']
        payment_method_id = serializer.validated_data.get('payment_method_id')

        stripe_service = StripeService()
        payment_intent = stripe_service.create_payment_intent(
            organization=wallet.organization,
            amount_cents=amount_cents,
            payment_method_id=payment_method_id,
        )

        return Response({
            'client_secret': payment_intent.client_secret,
            'payment_intent_id': payment_intent.id,
        })


class CreditLedgerViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = CreditLedgerSerializer
    lookup_field = 'id'

    def get_queryset(self):
        user_orgs = self.request.user.organizations.values_list('id', flat=True)
        wallet_ids = CreditWallet.objects.filter(organization_id__in=user_orgs).values_list('id', flat=True)
        return CreditLedger.objects.filter(wallet_id__in=wallet_ids).select_related('wallet', 'wallet__organization')


class InvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = InvoiceSerializer
    lookup_field = 'id'

    def get_queryset(self):
        user_orgs = self.request.user.organizations.values_list('id', flat=True)
        return Invoice.objects.filter(organization_id__in=user_orgs).select_related('organization', 'subscription')


class PaymentViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = PaymentSerializer
    lookup_field = 'id'

    def get_queryset(self):
        user_orgs = self.request.user.organizations.values_list('id', flat=True)
        return Payment.objects.filter(organization_id__in=user_orgs).select_related('organization', 'invoice', 'wallet')


class StripeWebhookView(APIView):
    """Handle Stripe webhooks"""
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request: Request):
        payload = request.body
        sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')
        endpoint_secret = settings.STRIPE_WEBHOOK_SECRET

        try:
            event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
        except ValueError:
            return Response({'detail': 'Invalid payload'}, status=status.HTTP_400_BAD_REQUEST)
        except stripe.error.SignatureVerificationError:
            return Response({'detail': 'Invalid signature'}, status=status.HTTP_400_BAD_REQUEST)

        # Process event
        stripe_service = StripeService()
        stripe_service.handle_webhook_event(event)

        # Enqueue for async processing
        enqueue_outbox_event(
            topic='billing.stripe.webhook',
            event_key=event.id,
            payload={'event_type': event.type, 'event_data': event.data.object},
            headers={'stripe_event_id': event.id}
        )

        return Response({'received': True})


class UsageView(APIView):
    """Get usage statistics for current organization"""
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        org = request.user.organizations.first()
        if not org:
            return Response({'detail': 'No organization found'}, status=status.HTTP_404_NOT_FOUND)

        wallet = CreditWallet.objects.filter(organization=org).first()
        subscription = Subscription.objects.filter(
            organization=org,
            status__in=[Subscription.Status.ACTIVE, Subscription.Status.TRIALING]
        ).first()

        # Get recent ledger entries
        recent_usage = CreditLedger.objects.filter(
            wallet=wallet,
            direction=CreditLedger.Direction.DEBIT
        ).order_by('-created_at')[:50]

        # Aggregate by reason
        from django.db.models import Sum
        usage_by_type = CreditLedger.objects.filter(
            wallet=wallet,
            direction=CreditLedger.Direction.DEBIT,
            created_at__gte=timezone.now() - timezone.timedelta(days=30)
        ).values('reason').annotate(total=Sum('credits')).order_by('-total')

        return Response({
            'wallet': CreditWalletSerializer(wallet).data if wallet else None,
            'subscription': SubscriptionSerializer(subscription).data if subscription else None,
            'recent_usage': CreditLedgerSerializer(recent_usage, many=True).data,
            'usage_by_type_30d': list(usage_by_type),
        })