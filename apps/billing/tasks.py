"""Billing background jobs: Stripe reconciliation, event retries, auto top-ups."""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def retry_failed_stripe_events() -> dict[str, int]:
    """Reprocess failed events until ``STRIPE_EVENT_MAX_ATTEMPTS``."""
    from apps.billing.models import StripeEvent
    from apps.billing.webhooks import process_event

    counts = {"processed": 0, "failed": 0}
    failed = StripeEvent.objects.filter(
        status=StripeEvent.Status.FAILED, attempts__lt=settings.STRIPE_EVENT_MAX_ATTEMPTS
    ).order_by("stripe_created")[:200]
    for row in failed:
        outcome = process_event(row.id)
        counts["processed" if outcome == StripeEvent.Status.PROCESSED else "failed"] += 1
    return counts


def catch_up_events(*, since_hours: int = 72) -> int:
    """Fetch recent Stripe events and process any webhook delivery that was missed."""
    import stripe

    from apps.billing.models import StripeEvent
    from apps.billing.stripe_client import _call
    from apps.billing.webhooks import HANDLERS, process_event, record_event

    since = int((timezone.now() - timedelta(hours=since_hours)).timestamp())
    events = _call(stripe.Event.list, created={"gte": since}, types=list(HANDLERS)[:20], limit=100)
    known = set(
        StripeEvent.objects.filter(
            stripe_created__gte=timezone.now() - timedelta(hours=since_hours)
        ).values_list("event_id", flat=True)
    )
    recovered = 0
    for event in events.auto_paging_iter():
        if event.id in known:
            continue
        row, created = record_event(event.to_dict_recursive())
        if created:
            process_event(row.id)
            recovered += 1
    return recovered


def reconcile_subscriptions() -> int:
    """Re-apply every customer's Stripe subscriptions to local state (Stripe is authoritative)."""
    import stripe

    from apps.billing.models import BillingCustomer
    from apps.billing.stripe_client import _call
    from apps.billing.webhooks import UnresolvableEvent, apply_subscription

    synced = 0
    for customer in BillingCustomer.objects.all().iterator():
        subscriptions = _call(
            stripe.Subscription.list,
            customer=customer.stripe_customer_id,
            status="all",
            limit=20,
            expand=["data.items.data.price"],
        )
        for subscription in subscriptions.auto_paging_iter():
            try:
                apply_subscription(subscription, event_created=timezone.now())
                synced += 1
            except UnresolvableEvent as exc:
                logger.warning("Subscription reconciliation skipped", extra={"reason": str(exc)})
    return synced


def reconcile_pending_payments(*, older_than_minutes: int = 30) -> int:
    """Resolve top-up payments whose webhook never arrived by asking Stripe."""
    import stripe

    from apps.billing.models import Payment
    from apps.billing.stripe_client import _call
    from apps.billing.webhooks import _on_payment_intent_failed, _on_payment_intent_succeeded

    resolved = 0
    cutoff = timezone.now() - timedelta(minutes=older_than_minutes)
    for payment in Payment.objects.filter(status=Payment.Status.PENDING, created_at__lt=cutoff)[:200]:
        intent = _call(stripe.PaymentIntent.retrieve, payment.provider_payment_id).to_dict_recursive()
        if intent.get("status") == "succeeded":
            _on_payment_intent_succeeded(intent, timezone.now())
            resolved += 1
        elif intent.get("status") in {"canceled", "requires_payment_method"} and payment.created_at < cutoff:
            _on_payment_intent_failed(intent, timezone.now())
            resolved += 1
    return resolved


@shared_task
def reconcile_stripe_billing() -> dict[str, int]:
    """Hourly: missed events, subscription state and pending payments against Stripe."""
    from apps.billing.stripe_client import stripe_configured

    if not stripe_configured():
        return {"events": 0, "subscriptions": 0, "payments": 0}
    return {
        "events": catch_up_events(),
        "subscriptions": reconcile_subscriptions(),
        "payments": reconcile_pending_payments(),
    }


@shared_task
def run_auto_topups() -> dict[str, int]:
    """Charge saved payment methods for wallets below their auto top-up threshold."""
    from django.db.models import F

    from apps.billing.models import CreditWallet
    from apps.billing.stripe_client import BillingNotConfigured, BillingProviderError, stripe_configured
    from apps.billing.stripe_client import create_topup_intent as topup

    if not stripe_configured():
        return {"charged": 0, "failed": 0}
    cooldown = timezone.now() - timedelta(minutes=settings.BILLING_AUTO_TOPUP_COOLDOWN_MINUTES)
    wallets = (
        CreditWallet.objects.filter(auto_topup_enabled=True, auto_topup_amount__gt=0)
        .filter(balance__lt=F("reserved_balance") + F("auto_topup_threshold"))
        .exclude(last_topup_at__gte=cooldown)
        .select_related("organization")
    )
    counts = {"charged": 0, "failed": 0}
    window = timezone.now().strftime("%Y%m%d%H")
    for wallet in wallets[:100]:
        cents = int(wallet.auto_topup_amount * wallet.credit_value_usd * 100)
        try:
            topup(
                wallet.organization,
                cents,
                off_session=True,
                idempotency_key=f"jt-auto-topup-{wallet.id}-{window}",
            )
            CreditWallet.objects.filter(id=wallet.id).update(last_topup_at=timezone.now())
            counts["charged"] += 1
        except (BillingNotConfigured, BillingProviderError) as exc:
            logger.warning("Auto top-up failed", extra={"wallet_id": str(wallet.id), "reason": str(exc)})
            counts["failed"] += 1
    return counts


@shared_task
def check_subscription_renewals() -> int:
    """Emit renewal notices for subscriptions renewing within seven days."""
    from apps.billing.models import Subscription
    from apps.events.outbox import enqueue_outbox_event

    soon = timezone.now() + timedelta(days=7)
    expiring = Subscription.objects.filter(
        status=Subscription.Status.ACTIVE, cancel_at_period_end=False, current_period_end__lte=soon
    ).select_related("plan")
    count = 0
    for subscription in expiring:
        enqueue_outbox_event(
            topic="billing.subscription.renewing_soon",
            event_key=f"{subscription.id}-{subscription.current_period_end.date()}",
            payload={
                "subscription_id": str(subscription.id),
                "organization_id": str(subscription.organization_id),
                "plan_name": subscription.plan.name,
                "renews_at": subscription.current_period_end.isoformat(),
            },
        )
        count += 1
    return count


@shared_task
def grant_free_plan_credits() -> int:
    """Grant the free plan's monthly credits once per calendar month to unsubscribed tenants."""
    from apps.billing.models import CreditLedger, Plan, Subscription
    from apps.billing.services import CreditService
    from apps.identity.models import Organization

    plan = Plan.objects.filter(slug=settings.BILLING_DEFAULT_PLAN, status=Plan.Status.ACTIVE).first()
    if plan is None or plan.monthly_credits <= 0 or not plan.is_free:
        return 0
    paying = Subscription.objects.filter(
        status__in=[Subscription.Status.ACTIVE, Subscription.Status.TRIALING, Subscription.Status.PAST_DUE]
    ).values_list("organization_id", flat=True)
    month = timezone.now().strftime("%Y-%m")
    granted = 0
    for organization in Organization.objects.exclude(id__in=paying).iterator():
        key = f"free_grant_{organization.id}_{month}"
        wallet = CreditService.get_or_create_wallet(organization)
        if CreditLedger.objects.filter(wallet=wallet, idempotency_key=key).exists():
            continue
        CreditService.add_credits(
            wallet,
            plan.monthly_credits,
            reason=f"{plan.name} plan credits for {month}",
            ledger_reason=CreditLedger.Reason.SUBSCRIPTION_GRANT,
            idempotency_key=key,
        )
        granted += 1
    return granted
