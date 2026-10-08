"""Idempotent processing of verified Stripe events.

Each event is stored once (``StripeEvent.event_id`` is unique) and processed
under that row's lock: duplicate or concurrent deliveries are no-ops once an
event is processed. Subscription state is re-read from Stripe (the source of
truth) instead of trusting event order, and every credit grant or reversal uses
a deterministic ledger idempotency key.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.billing.models import (
    BillingCustomer,
    CreditLedger,
    CreditWallet,
    Invoice,
    Payment,
    Plan,
    StripeEvent,
    Subscription,
)
from apps.billing.services import CreditService

logger = logging.getLogger(__name__)

_SUBSCRIPTION_STATUS = {
    "active": Subscription.Status.ACTIVE,
    "trialing": Subscription.Status.TRIALING,
    "past_due": Subscription.Status.PAST_DUE,
    "unpaid": Subscription.Status.PAST_DUE,
    "canceled": Subscription.Status.CANCELED,
    "incomplete": Subscription.Status.INCOMPLETE,
    "incomplete_expired": Subscription.Status.CANCELED,
    "paused": Subscription.Status.PAUSED,
}
_INVOICE_STATUS = {
    "draft": Invoice.Status.DRAFT,
    "open": Invoice.Status.OPEN,
    "paid": Invoice.Status.PAID,
    "void": Invoice.Status.VOID,
    "uncollectible": Invoice.Status.UNCOLLECTIBLE,
}
_GRANTING_BILLING_REASONS = {"subscription_create", "subscription_cycle"}


class UnresolvableEvent(ValueError):
    """The event refers to objects this deployment cannot map (it is retried, then surfaced)."""


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), tz=UTC) if value else None


def _organization_for(obj: dict[str, Any]) -> Any:
    from apps.identity.models import Organization

    organization_id = (obj.get("metadata") or {}).get("organization_id")
    if organization_id:
        organization = Organization.objects.filter(id=organization_id).first()
        if organization is not None:
            return organization
    customer_id = obj.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")
    customer = (
        BillingCustomer.objects.filter(stripe_customer_id=customer_id).select_related("organization").first()
    )
    return customer.organization if customer else None


def apply_subscription(
    stripe_subscription: dict[str, Any], *, event_created: datetime | None = None
) -> Subscription:
    """Upsert the local subscription from an authoritative Stripe subscription."""
    organization = _organization_for(stripe_subscription)
    if organization is None:
        raise UnresolvableEvent(f"No organization for Stripe subscription {stripe_subscription.get('id')}.")
    items = ((stripe_subscription.get("items") or {}).get("data")) or []
    price = (items[0].get("price") if items else None) or {}
    price_id = price.get("id", "") if isinstance(price, dict) else str(price)
    plan = Plan.objects.filter(
        Q(stripe_price_monthly_id=price_id) | Q(stripe_price_yearly_id=price_id)
    ).first()
    if plan is None and (plan_id := (stripe_subscription.get("metadata") or {}).get("plan_id")):
        plan = Plan.objects.filter(id=plan_id).first()
    if plan is None:
        raise UnresolvableEvent(f"Stripe price {price_id!r} is not linked to a plan; run sync_stripe_prices.")
    recurring = price.get("recurring") if isinstance(price, dict) else None
    interval = (recurring or {}).get("interval") or (
        "year" if price_id == plan.stripe_price_yearly_id else "month"
    )
    with transaction.atomic():
        subscription = (
            Subscription.objects.select_for_update()
            .filter(provider_subscription_id=stripe_subscription["id"])
            .first()
        )
        if (
            subscription is not None
            and event_created is not None
            and subscription.provider_updated_at is not None
            and event_created < subscription.provider_updated_at
        ):
            return subscription  # an older snapshot than the one already applied
        values = {
            "organization": organization,
            "plan": plan,
            "status": _SUBSCRIPTION_STATUS.get(
                stripe_subscription.get("status", ""), Subscription.Status.INCOMPLETE
            ),
            "provider": "stripe",
            "provider_customer_id": str(stripe_subscription.get("customer") or ""),
            "current_period_start": _ts(stripe_subscription.get("current_period_start")) or timezone.now(),
            "current_period_end": _ts(stripe_subscription.get("current_period_end")) or timezone.now(),
            "cancel_at_period_end": bool(stripe_subscription.get("cancel_at_period_end")),
            "canceled_at": _ts(stripe_subscription.get("canceled_at")),
            "trial_start": _ts(stripe_subscription.get("trial_start")),
            "trial_end": _ts(stripe_subscription.get("trial_end")),
            "interval": interval,
            "stripe_price_id": price_id,
            "provider_updated_at": event_created or timezone.now(),
        }
        if subscription is None:
            subscription = Subscription.objects.create(
                provider_subscription_id=stripe_subscription["id"], **values
            )
        else:
            for field, value in values.items():
                setattr(subscription, field, value)
            subscription.save()
    return subscription


def _subscription_from_stripe(subscription_id: str, event_created: datetime | None) -> Subscription:
    from apps.billing.stripe_client import retrieve_subscription

    return apply_subscription(retrieve_subscription(subscription_id), event_created=event_created)


def _on_subscription(obj: dict[str, Any], event_created: datetime) -> None:
    # Re-read the subscription so delayed or reordered events cannot regress it.
    _subscription_from_stripe(obj["id"], event_created)


def _on_checkout_completed(obj: dict[str, Any], event_created: datetime) -> None:
    if obj.get("mode") == "subscription" and obj.get("subscription"):
        _subscription_from_stripe(str(obj["subscription"]), event_created)


def upsert_invoice(obj: dict[str, Any]) -> Invoice:
    subscription = None
    if obj.get("subscription"):
        subscription = Subscription.objects.filter(provider_subscription_id=obj["subscription"]).first()
    organization = subscription.organization if subscription else _organization_for(obj)
    if organization is None:
        raise UnresolvableEvent(f"No organization for Stripe invoice {obj.get('id')}.")
    lines = ((obj.get("lines") or {}).get("data")) or []
    period = (lines[0].get("period") if lines else None) or {}
    paid_at = _ts((obj.get("status_transitions") or {}).get("paid_at"))
    invoice, _ = Invoice.objects.update_or_create(
        provider_invoice_id=obj["id"],
        defaults={
            "organization": organization,
            "subscription": subscription,
            "provider": "stripe",
            "number": obj.get("number") or "",
            "description": (obj.get("description") or (lines[0].get("description") if lines else "") or "")[
                :500
            ],
            "status": _INVOICE_STATUS.get(obj.get("status", ""), Invoice.Status.OPEN),
            "amount_cents": int(obj.get("amount_due") or obj.get("total") or 0),
            "amount_paid_cents": int(obj.get("amount_paid") or 0),
            "currency": str(obj.get("currency") or "usd").upper(),
            "period_start": _ts(period.get("start") or obj.get("period_start")) or timezone.now(),
            "period_end": _ts(period.get("end") or obj.get("period_end")) or timezone.now(),
            "due_date": _ts(obj.get("due_date")),
            "paid_at": paid_at,
            "invoice_pdf_url": obj.get("invoice_pdf") or "",
            "hosted_invoice_url": obj.get("hosted_invoice_url") or "",
        },
    )
    return invoice


def _on_invoice(obj: dict[str, Any], event_created: datetime) -> None:
    upsert_invoice(obj)


def _on_invoice_paid(obj: dict[str, Any], event_created: datetime) -> None:
    if (
        obj.get("subscription")
        and not Subscription.objects.filter(provider_subscription_id=obj["subscription"]).exists()
    ):
        _subscription_from_stripe(str(obj["subscription"]), event_created)
    invoice = upsert_invoice(obj)
    subscription = invoice.subscription
    if subscription is None or obj.get("billing_reason") not in _GRANTING_BILLING_REASONS:
        return
    months = 12 if subscription.interval == "year" else 1
    amount = subscription.plan.monthly_credits * months
    if amount <= 0:
        return
    wallet = CreditService.get_or_create_wallet(subscription.organization)
    CreditService.add_credits(
        wallet,
        amount,
        reason=f"{subscription.plan.name} plan credits ({invoice.number or invoice.provider_invoice_id})",
        request_id=subscription.id,
        ledger_reason=CreditLedger.Reason.SUBSCRIPTION_GRANT,
        idempotency_key=f"invoice_grant_{obj['id']}",
        metadata={"invoiceId": obj["id"], "months": months},
    )


def _on_invoice_failed(obj: dict[str, Any], event_created: datetime) -> None:
    upsert_invoice(obj)
    if obj.get("subscription"):
        _subscription_from_stripe(str(obj["subscription"]), event_created)


def _on_payment_intent_succeeded(obj: dict[str, Any], event_created: datetime) -> None:
    metadata = obj.get("metadata") or {}
    if metadata.get("kind") not in {"topup", "auto_topup"}:
        return
    organization = _organization_for(obj)
    if organization is None:
        raise UnresolvableEvent(f"No organization for PaymentIntent {obj.get('id')}.")
    credits = Decimal(str(metadata.get("credits") or "0"))
    if credits <= 0:
        from apps.billing.stripe_client import credits_for_payment

        credits = credits_for_payment(int(obj.get("amount_received") or obj.get("amount") or 0))
    wallet = CreditService.get_or_create_wallet(organization)
    payment, _ = Payment.objects.get_or_create(
        provider_payment_id=obj["id"],
        defaults={
            "organization": organization,
            "wallet": wallet,
            "provider": "stripe",
            "type": Payment.Type.TOPUP,
            "amount_cents": int(obj.get("amount") or 0),
            "currency": str(obj.get("currency") or "usd").upper(),
            "idempotency_key": f"topup_{obj['id']}",
        },
    )
    CreditService.add_credits(
        wallet,
        credits,
        reason=f"Credit top-up (${int(obj.get('amount_received') or obj.get('amount') or 0) / 100:.2f})",
        ledger_reason=(
            CreditLedger.Reason.AUTO_TOPUP
            if metadata.get("kind") == "auto_topup"
            else CreditLedger.Reason.MANUAL_TOPUP
        ),
        idempotency_key=f"topup_{obj['id']}",
        metadata={"stripePaymentIntentId": obj["id"]},
    )
    payment.status = Payment.Status.SUCCEEDED
    payment.wallet = wallet
    payment.credits_granted = credits
    payment.succeeded_at = payment.succeeded_at or timezone.now()
    payment.save(update_fields=["status", "wallet", "credits_granted", "succeeded_at", "updated_at"])
    if metadata.get("kind") == "auto_topup":
        CreditWallet.objects.filter(id=wallet.id).update(last_topup_at=timezone.now())


def _on_payment_intent_failed(obj: dict[str, Any], event_created: datetime) -> None:
    error = obj.get("last_payment_error") or {}
    Payment.objects.filter(provider_payment_id=obj["id"]).exclude(status=Payment.Status.SUCCEEDED).update(
        status=Payment.Status.FAILED,
        failure_code=str(error.get("code") or "")[:100],
        failure_message=str(error.get("message") or "")[:2000],
        updated_at=timezone.now(),
    )


def _on_charge_refunded(obj: dict[str, Any], event_created: datetime) -> None:
    """Reverse top-up credits in proportion to the refunded amount (never below zero)."""
    payment = Payment.objects.filter(
        provider_payment_id=obj.get("payment_intent"), type=Payment.Type.TOPUP
    ).first()
    if payment is None or payment.amount_cents <= 0:
        return
    refunded = int(obj.get("amount_refunded") or 0)
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(id=payment.id)
        delta = refunded - payment.refunded_cents
        if delta <= 0:
            return
        wallet = CreditWallet.objects.select_for_update().get(organization=payment.organization)
        due = (payment.credits_granted * Decimal(delta) / Decimal(payment.amount_cents)).quantize(
            Decimal("0.000001")
        )
        reversed_credits = max(Decimal("0"), min(due, wallet.balance - wallet.reserved_balance))
        if reversed_credits > 0:
            wallet.balance -= reversed_credits
            wallet.save(update_fields=["balance", "updated_at"])
            CreditLedger.objects.create(
                wallet=wallet,
                direction=CreditLedger.Direction.DEBIT,
                credits=reversed_credits,
                reason=CreditLedger.Reason.REFUND,
                description=f"Refund of top-up {payment.provider_payment_id}",
                idempotency_key=f"refund_{obj['id']}_{refunded}",
                balance_after=wallet.balance,
                metadata={"chargeId": obj["id"], "dueCredits": str(due)},
            )
        payment.refunded_cents = refunded
        payment.credits_reversed += reversed_credits
        if refunded >= payment.amount_cents:
            payment.status = Payment.Status.REFUNDED
        payment.metadata = {**payment.metadata, "unreversedCredits": str(due - reversed_credits)}
        payment.save(update_fields=["refunded_cents", "credits_reversed", "status", "metadata", "updated_at"])


def _on_setup_intent_succeeded(obj: dict[str, Any], event_created: datetime) -> None:
    from apps.billing.stripe_client import set_default_payment_method

    customer = BillingCustomer.objects.filter(stripe_customer_id=obj.get("customer")).first()
    if customer is None or not obj.get("payment_method"):
        raise UnresolvableEvent(f"No billing customer for SetupIntent {obj.get('id')}.")
    set_default_payment_method(customer, str(obj["payment_method"]))


HANDLERS: dict[str, Callable[[dict[str, Any], datetime], None]] = {
    "checkout.session.completed": _on_checkout_completed,
    "customer.subscription.created": _on_subscription,
    "customer.subscription.updated": _on_subscription,
    "customer.subscription.deleted": _on_subscription,
    "customer.subscription.paused": _on_subscription,
    "customer.subscription.resumed": _on_subscription,
    "invoice.paid": _on_invoice_paid,
    "invoice.payment_succeeded": _on_invoice_paid,
    "invoice.payment_failed": _on_invoice_failed,
    "invoice.finalized": _on_invoice,
    "invoice.updated": _on_invoice,
    "invoice.voided": _on_invoice,
    "invoice.marked_uncollectible": _on_invoice,
    "payment_intent.succeeded": _on_payment_intent_succeeded,
    "payment_intent.payment_failed": _on_payment_intent_failed,
    "charge.refunded": _on_charge_refunded,
    "setup_intent.succeeded": _on_setup_intent_succeeded,
}


def record_event(event: dict[str, Any]) -> tuple[StripeEvent, bool]:
    """Store a verified event exactly once."""
    return StripeEvent.objects.get_or_create(
        event_id=event["id"],
        defaults={
            "event_type": event["type"],
            "livemode": bool(event.get("livemode")),
            "stripe_created": _ts(event.get("created")) or timezone.now(),
            "payload": event,
        },
    )


def process_event(event_row_id: Any) -> str:
    """Process a stored event once; returns the resulting status."""
    with transaction.atomic():
        row = StripeEvent.objects.select_for_update().get(id=event_row_id)
        if row.status in {StripeEvent.Status.PROCESSED, StripeEvent.Status.IGNORED}:
            return row.status
        row.attempts += 1
        handler = HANDLERS.get(row.event_type)
        if handler is None:
            row.status = StripeEvent.Status.IGNORED
        else:
            try:
                with transaction.atomic():
                    handler(row.payload["data"]["object"], row.stripe_created)
                row.status = StripeEvent.Status.PROCESSED
                row.last_error = ""
            except Exception as exc:  # noqa: BLE001 - recorded and retried; partial work rolled back
                logger.exception("Stripe event processing failed", extra={"stripe_event_id": row.event_id})
                row.status = StripeEvent.Status.FAILED
                row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        row.processed_at = timezone.now() if row.status != StripeEvent.Status.FAILED else None
        row.save(update_fields=["status", "attempts", "last_error", "processed_at"])
        return row.status
