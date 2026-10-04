"""Stripe API operations (Checkout, Payment/Setup Intents, Portal, subscriptions).

All calls use the pinned ``STRIPE_API_VERSION`` and Stripe idempotency keys so a
retried request never creates a second customer, session or charge. Card data
never touches this server: payment methods are collected by Stripe (Checkout,
Elements with a SetupIntent, or the Billing Portal).
"""

from __future__ import annotations

import uuid
from decimal import ROUND_DOWN, Decimal
from typing import Any
from urllib.parse import urlsplit

import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError

from apps.billing.models import BillingCustomer, Payment, Plan


class BillingNotConfigured(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Billing is not configured."
    default_code = "billing_not_configured"


class BillingProviderError(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "The payment provider rejected the request."
    default_code = "billing_provider_error"


def stripe_configured() -> bool:
    key = settings.STRIPE_SECRET_KEY or ""
    return key.startswith(("sk_", "rk_")) and "replace_me" not in key


def configure() -> None:
    if not stripe_configured():
        raise BillingNotConfigured()
    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.api_version = settings.STRIPE_API_VERSION
    stripe.max_network_retries = 2


def _call(operation: Any, *args: Any, **kwargs: Any) -> Any:
    configure()
    try:
        return operation(*args, **kwargs)
    except stripe.CardError as exc:
        raise BillingProviderError(detail=exc.user_message or "The card was declined.") from exc
    except stripe.StripeError as exc:
        raise BillingProviderError(detail=f"Stripe error: {exc.user_message or type(exc).__name__}") from exc


def allowed_origins() -> set[str]:
    origins = {
        settings.FRONTEND_URL.rstrip("/"),
        *(origin.rstrip("/") for origin in settings.CORS_ALLOWED_ORIGINS),
    }
    return {origin for origin in origins if origin}


def safe_return_url(candidate: str | None, default_path: str) -> str:
    """Accept a client return URL only on an allowed origin (no open redirects)."""
    default = f"{settings.FRONTEND_URL.rstrip('/')}{default_path}"
    if not candidate:
        return default
    parts = urlsplit(candidate)
    if f"{parts.scheme}://{parts.netloc}" not in allowed_origins():
        raise ValidationError({"returnUrl": "The return URL must be on the application's origin."})
    return candidate


def get_or_create_customer(organization: Any) -> BillingCustomer:
    existing = BillingCustomer.objects.filter(organization=organization).first()
    if existing is not None:
        return existing
    owner = getattr(organization, "owner", None)
    customer = _call(
        stripe.Customer.create,
        name=organization.name,
        email=getattr(owner, "email", "") or None,
        metadata={"organization_id": str(organization.id)},
        idempotency_key=f"jt-customer-{organization.id}",
    )
    try:
        with transaction.atomic():
            return BillingCustomer.objects.create(organization=organization, stripe_customer_id=customer.id)
    except IntegrityError:
        return BillingCustomer.objects.get(organization=organization)


def create_subscription_checkout(
    organization: Any, plan: Plan, interval: str, *, success_url: str, cancel_url: str
) -> Any:
    price = plan.stripe_price_for(interval)
    if not price:
        raise BillingNotConfigured(
            detail=f"The {plan.name} plan has no Stripe {interval}ly price; run manage.py sync_stripe_prices."
        )
    customer = get_or_create_customer(organization)
    metadata = {"organization_id": str(organization.id), "plan_id": str(plan.id), "interval": interval}
    return _call(
        stripe.checkout.Session.create,
        mode="subscription",
        customer=customer.stripe_customer_id,
        client_reference_id=str(organization.id),
        line_items=[{"price": price, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        allow_promotion_codes=True,
        metadata=metadata,
        subscription_data={"metadata": metadata},
        idempotency_key=f"jt-checkout-{uuid.uuid4()}",
    )


def credits_for_payment(amount_cents: int) -> Decimal:
    """Credits bought for ``amount_cents`` at the current credit value (snapshotted at purchase)."""
    value = Decimal(str(settings.BILLING_CREDIT_VALUE_USD))
    return (Decimal(amount_cents) / Decimal(100) / value).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)


def create_topup_intent(
    organization: Any, amount_cents: int, *, off_session: bool = False, idempotency_key: str | None = None
) -> Any:
    if not settings.BILLING_TOPUP_MIN_CENTS <= amount_cents <= settings.BILLING_TOPUP_MAX_CENTS:
        raise ValidationError(
            {
                "amount_cents": (
                    f"Top-ups must be between {settings.BILLING_TOPUP_MIN_CENTS} and "
                    f"{settings.BILLING_TOPUP_MAX_CENTS} cents."
                )
            }
        )
    customer = get_or_create_customer(organization)
    credits = credits_for_payment(amount_cents)
    options: dict[str, Any] = {
        "amount": amount_cents,
        "currency": "usd",
        "customer": customer.stripe_customer_id,
        "metadata": {
            "organization_id": str(organization.id),
            "kind": "auto_topup" if off_session else "topup",
            "credits": str(credits),
        },
        "idempotency_key": idempotency_key or f"jt-topup-{uuid.uuid4()}",
    }
    if off_session:
        if not customer.default_payment_method_id:
            raise BillingNotConfigured(detail="No saved payment method for automatic top-ups.")
        options.update(payment_method=customer.default_payment_method_id, off_session=True, confirm=True)
    else:
        options["automatic_payment_methods"] = {"enabled": True}
    intent = _call(stripe.PaymentIntent.create, **options)
    Payment.objects.get_or_create(
        provider_payment_id=intent.id,
        defaults={
            "organization": organization,
            "wallet": getattr(organization, "credit_wallet", None),
            "provider": "stripe",
            "type": Payment.Type.TOPUP,
            "status": Payment.Status.PENDING,
            "amount_cents": amount_cents,
            "currency": "USD",
            "credits_granted": Decimal("0"),
            "idempotency_key": f"topup_{intent.id}",
            "metadata": {"credits": str(credits), "offSession": off_session},
        },
    )
    return intent


def create_setup_intent(organization: Any) -> Any:
    customer = get_or_create_customer(organization)
    return _call(
        stripe.SetupIntent.create,
        customer=customer.stripe_customer_id,
        usage="off_session",
        automatic_payment_methods={"enabled": True},
        metadata={"organization_id": str(organization.id)},
        idempotency_key=f"jt-setup-{uuid.uuid4()}",
    )


def create_portal_session(organization: Any, return_url: str) -> Any:
    customer = get_or_create_customer(organization)
    return _call(
        stripe.billing_portal.Session.create, customer=customer.stripe_customer_id, return_url=return_url
    )


def set_cancel_at_period_end(provider_subscription_id: str, cancel: bool) -> Any:
    return _call(
        stripe.Subscription.modify,
        provider_subscription_id,
        cancel_at_period_end=cancel,
        idempotency_key=f"jt-sub-cancel-{provider_subscription_id}-{int(cancel)}-{uuid.uuid4()}",
    )


def retrieve_subscription(provider_subscription_id: str) -> Any:
    return _call(stripe.Subscription.retrieve, provider_subscription_id, expand=["items.data.price"])


def payment_method_summary(payment_method_id: str) -> dict[str, Any]:
    method = _call(stripe.PaymentMethod.retrieve, payment_method_id)
    card = getattr(method, "card", None)
    billing = getattr(method, "billing_details", None)
    return {
        "id": method.id,
        "brand": getattr(card, "brand", "") or method.type,
        "last4": getattr(card, "last4", "") or "",
        "expMonth": getattr(card, "exp_month", None),
        "expYear": getattr(card, "exp_year", None),
        "name": getattr(billing, "name", "") or "",
    }


def set_default_payment_method(customer: BillingCustomer, payment_method_id: str) -> None:
    _call(
        stripe.Customer.modify,
        customer.stripe_customer_id,
        invoice_settings={"default_payment_method": payment_method_id},
    )
    customer.default_payment_method_id = payment_method_id
    customer.payment_method_summary = payment_method_summary(payment_method_id)
    customer.save(update_fields=["default_payment_method_id", "payment_method_summary", "updated_at"])
