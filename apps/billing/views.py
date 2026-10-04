"""Billing API: plans, Stripe Checkout/Portal, subscriptions, wallet, invoices, webhooks."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import stripe
from django.conf import settings
from django.http import JsonResponse, StreamingHttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.billing import stripe_client
from apps.billing.models import (
    BillingCustomer,
    CreditLedger,
    Invoice,
    Payment,
    Plan,
    Subscription,
)
from apps.billing.serializers import (
    CreditLedgerSerializer,
    CreditWalletSerializer,
    InvoiceSerializer,
    PaymentSerializer,
    PlanSerializer,
    PortalSerializer,
    SubscribeSerializer,
    SubscriptionSerializer,
    TopUpSerializer,
    WalletSettingsSerializer,
)
from apps.billing.services import CreditService
from apps.billing.webhooks import apply_subscription, process_event, record_event
from apps.core.metrics import SECURITY_EVENTS, WEBHOOKS
from apps.core.views import APIView
from apps.events.outbox import enqueue_outbox_event
from apps.governance.audit import security_event
from apps.identity.authorization import organization_for_request, require_organization_write_access

_INVOICE_PDF_HOSTS = {"pay.stripe.com", "files.stripe.com", "invoice.stripe.com"}


def _organization(request: Request) -> Any:
    return organization_for_request(request, required=True)


def _current_subscription(organization: Any) -> Subscription | None:
    return (
        Subscription.objects.filter(
            organization=organization,
            status__in=[
                Subscription.Status.ACTIVE,
                Subscription.Status.TRIALING,
                Subscription.Status.PAST_DUE,
            ],
        )
        .select_related("plan")
        .order_by("-current_period_end")
        .first()
    )


class PlanViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = PlanSerializer
    lookup_field = "slug"

    def get_queryset(self):
        return Plan.objects.filter(status=Plan.Status.ACTIVE).prefetch_related("entitlements")

    @extend_schema(
        request=SubscribeSerializer,
        responses={
            200: inline_serializer(
                "CheckoutSession",
                {"checkoutUrl": serializers.URLField(), "sessionId": serializers.CharField()},
            )
        },
    )
    @action(detail=True, methods=["post"])
    def subscribe(self, request: Request, slug=None):
        """Start Stripe Checkout for this plan (the subscription activates via webhook)."""
        plan = self.get_object()
        organization = _organization(request)
        require_organization_write_access(request.user, organization.id)
        serializer = SubscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if plan.is_free:
            raise ValidationError({"plan": "The free plan needs no subscription."})
        if _current_subscription(organization) is not None:
            raise ValidationError(
                {"plan": "The organization already has a subscription; manage it in the portal."}
            )
        session = stripe_client.create_subscription_checkout(
            organization,
            plan,
            serializer.validated_data["interval"],
            success_url=stripe_client.safe_return_url(
                serializer.validated_data.get("successUrl"), "/billing?checkout=success"
            ),
            cancel_url=stripe_client.safe_return_url(
                serializer.validated_data.get("cancelUrl"), "/billing?checkout=cancelled"
            ),
        )
        return Response({"checkoutUrl": session.url, "sessionId": session.id})


class SubscriptionViewSet(viewsets.ReadOnlyModelViewSet):
    """Subscriptions are changed only through Stripe (Checkout, Portal, cancel/reactivate)."""

    permission_classes = [IsAuthenticated]
    serializer_class = SubscriptionSerializer
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Subscription.objects.none()
        return (
            Subscription.objects.filter(organization=_organization(self.request))
            .select_related("plan")
            .order_by("-created_at")
        )

    def _toggle(self, request: Request, *, cancel: bool) -> Response:
        organization = _organization(request)
        require_organization_write_access(request.user, organization.id)
        subscription = _current_subscription(organization)
        if subscription is None:
            raise NotFound("The organization has no active subscription.")
        if cancel == subscription.cancel_at_period_end:
            raise ValidationError(
                {
                    "subscription": "Already set to cancel."
                    if cancel
                    else "The subscription is not set to cancel."
                }
            )
        updated = stripe_client.set_cancel_at_period_end(subscription.provider_subscription_id, cancel)
        subscription = apply_subscription(updated, event_created=timezone.now())
        return Response(SubscriptionSerializer(subscription).data)

    @extend_schema(request=None, responses={200: SubscriptionSerializer})
    @action(detail=False, methods=["post"])
    def cancel(self, request: Request):
        """Cancel at the end of the current period (no proration; access continues until then)."""
        return self._toggle(request, cancel=True)

    @extend_schema(request=None, responses={200: SubscriptionSerializer})
    @action(detail=False, methods=["post"])
    def reactivate(self, request: Request):
        """Undo a pending cancellation before the period ends."""
        return self._toggle(request, cancel=False)


class WalletView(APIView):
    """The selected organization's credit wallet (``/wallets/me/``)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: CreditWalletSerializer})
    def get(self, request: Request) -> Response:
        wallet = CreditService.get_or_create_wallet(_organization(request))
        return Response(CreditWalletSerializer(wallet).data)

    @extend_schema(request=WalletSettingsSerializer, responses={200: CreditWalletSerializer})
    def patch(self, request: Request) -> Response:
        organization = _organization(request)
        require_organization_write_access(request.user, organization.id)
        serializer = WalletSettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        wallet = CreditService.get_or_create_wallet(organization)
        fields = []
        if "auto_topup_enabled" in data:
            if (
                data["auto_topup_enabled"]
                and not BillingCustomer.objects.filter(organization=organization)
                .exclude(default_payment_method_id="")
                .exists()
            ):
                raise ValidationError(
                    {"auto_topup_enabled": "Save a payment method before enabling auto top-up."}
                )
            wallet.auto_topup_enabled = data["auto_topup_enabled"]
            fields.append("auto_topup_enabled")
        if "auto_topup_threshold" in data:
            wallet.auto_topup_threshold = data["auto_topup_threshold"]
            fields.append("auto_topup_threshold")
        if "auto_topup_amount_cents" in data:
            cents = data["auto_topup_amount_cents"]
            if not settings.BILLING_TOPUP_MIN_CENTS <= cents <= settings.BILLING_TOPUP_MAX_CENTS:
                raise ValidationError({"auto_topup_amount_cents": "Outside the allowed top-up range."})
            wallet.auto_topup_amount = stripe_client.credits_for_payment(cents)
            fields.append("auto_topup_amount")
        if "monthly_spending_limit" in data:
            wallet.monthly_spending_limit = data["monthly_spending_limit"]
            fields.append("monthly_spending_limit")
        if fields:
            wallet.save(update_fields=[*fields, "updated_at"])
        return Response(CreditWalletSerializer(wallet).data)


class WalletTopUpView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=TopUpSerializer,
        responses={
            200: inline_serializer(
                "TopUpIntent",
                {
                    "clientSecret": serializers.CharField(),
                    "paymentIntentId": serializers.CharField(),
                    "credits": serializers.DecimalField(max_digits=20, decimal_places=6),
                    "publishableKey": serializers.CharField(),
                },
            )
        },
    )
    def post(self, request: Request) -> Response:
        """Create a PaymentIntent; credits are granted when Stripe confirms the payment."""
        organization = _organization(request)
        require_organization_write_access(request.user, organization.id)
        serializer = TopUpSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        amount = serializer.validated_data["amount_cents"]
        intent = stripe_client.create_topup_intent(organization, amount)
        return Response(
            {
                "clientSecret": intent.client_secret,
                "paymentIntentId": intent.id,
                "credits": stripe_client.credits_for_payment(amount),
                "publishableKey": settings.STRIPE_PUBLISHABLE_KEY,
            }
        )


class PaymentMethodView(APIView):
    """``GET /payment-methods/me/`` summary; ``POST /payment-methods/`` starts a SetupIntent."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request: Request) -> Response | JsonResponse:
        customer = BillingCustomer.objects.filter(organization=_organization(request)).first()
        if customer is None or not customer.default_payment_method_id:
            return JsonResponse(None, safe=False)  # a JSON ``null`` body, per the frontend contract
        return Response(customer.payment_method_summary or {"id": customer.default_payment_method_id})

    @extend_schema(
        request=None,
        responses={
            200: inline_serializer(
                "SetupIntent",
                {
                    "clientSecret": serializers.CharField(),
                    "setupIntentId": serializers.CharField(),
                    "publishableKey": serializers.CharField(),
                },
            )
        },
    )
    def post(self, request: Request) -> Response:
        if any(key in request.data for key in ("number", "cardNumber", "cvc", "card_number")):
            raise ValidationError({"detail": "Card details must be submitted to Stripe, never to this API."})
        organization = _organization(request)
        require_organization_write_access(request.user, organization.id)
        intent = stripe_client.create_setup_intent(organization)
        return Response(
            {
                "clientSecret": intent.client_secret,
                "setupIntentId": intent.id,
                "publishableKey": settings.STRIPE_PUBLISHABLE_KEY,
            }
        )


class BillingPortalView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=PortalSerializer,
        responses={200: inline_serializer("PortalSession", {"url": serializers.URLField()})},
    )
    def post(self, request: Request) -> Response:
        organization = _organization(request)
        require_organization_write_access(request.user, organization.id)
        serializer = PortalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return_url = stripe_client.safe_return_url(serializer.validated_data.get("returnUrl"), "/billing")
        return Response({"url": stripe_client.create_portal_session(organization, return_url).url})


class CreditLedgerViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = CreditLedgerSerializer
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return CreditLedger.objects.none()
        return CreditLedger.objects.filter(wallet__organization=_organization(self.request)).order_by(
            "-created_at"
        )


class InvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    """Invoices (newest 100 as an array, per the frontend contract)."""

    permission_classes = [IsAuthenticated]
    serializer_class = InvoiceSerializer
    lookup_field = "id"
    pagination_class = None

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Invoice.objects.none()
        queryset = Invoice.objects.filter(organization=_organization(self.request)).order_by("-created_at")
        return queryset[:100] if self.action == "list" else queryset

    @extend_schema(responses={(200, "application/pdf"): OpenApiTypes.BINARY})
    @action(detail=True, methods=["get"])
    def download(self, request: Request, id=None):
        """Stream the Stripe-hosted invoice PDF (only from Stripe's hosts)."""
        import httpx

        invoice = self.get_object()
        url = invoice.invoice_pdf_url
        if not url or urlsplit(url).hostname not in _INVOICE_PDF_HOSTS:
            raise NotFound("No PDF is available for this invoice yet.")

        def chunks():
            with (
                httpx.Client(timeout=30, follow_redirects=True, max_redirects=3) as client,
                client.stream("GET", url) as response,
            ):
                response.raise_for_status()
                if urlsplit(str(response.url)).hostname not in _INVOICE_PDF_HOSTS:
                    raise ValueError("Unexpected invoice PDF host.")
                yield from response.iter_bytes()

        response = StreamingHttpResponse(chunks(), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="invoice-{invoice.number or invoice.id}.pdf"'
        return response


class PaymentViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = PaymentSerializer
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Payment.objects.none()
        return Payment.objects.filter(organization=_organization(self.request)).order_by("-created_at")


class StripeWebhookView(APIView):
    """Verify, store once, and process Stripe events (duplicates are acknowledged no-ops)."""

    permission_classes = [AllowAny]
    authentication_classes: list[Any] = []
    throttle_classes: list[Any] = []

    @extend_schema(request=None, responses={200: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        try:
            event = stripe.Webhook.construct_event(
                request.body,
                request.META.get("HTTP_STRIPE_SIGNATURE", ""),
                settings.STRIPE_WEBHOOK_SECRET,
                tolerance=settings.STRIPE_WEBHOOK_TOLERANCE_SECONDS,
            )
        except ValueError:
            WEBHOOKS.labels("stripe", "invalid_payload").inc()
            return Response({"detail": "Invalid payload"}, status=status.HTTP_400_BAD_REQUEST)
        except stripe.SignatureVerificationError:
            # Covers forged signatures and replays outside the timestamp tolerance.
            WEBHOOKS.labels("stripe", "invalid").inc()
            SECURITY_EVENTS.labels("webhook_invalid").inc()
            security_event(
                "webhook.rejected",
                resource_type="stripe",
                description="Rejected Stripe webhook: invalid or expired signature",
                request=request._request,
                reason="invalid",
            )
            return Response({"detail": "Invalid signature"}, status=status.HTTP_400_BAD_REQUEST)
        payload = event.to_dict_recursive() if hasattr(event, "to_dict_recursive") else dict(event)
        row, created = record_event(payload)
        WEBHOOKS.labels("stripe", "accepted" if created else "duplicate").inc()
        if created:
            enqueue_outbox_event(
                topic="billing.stripe.webhook",
                event_key=row.event_id,
                payload={"event_id": row.event_id, "event_type": row.event_type},
                headers={"stripe_event_id": row.event_id},
            )
        outcome = process_event(row.id)
        if outcome == "failed":
            # A non-2xx makes Stripe redeliver; the retry task also reprocesses failures.
            return Response({"detail": "Event processing failed; it will be retried."}, status=500)
        return Response({"received": True, "status": outcome, "duplicate": not created})
