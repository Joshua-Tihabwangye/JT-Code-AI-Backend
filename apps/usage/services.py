"""Metering: reserve before billable work, settle from actual usage afterwards.

Every reservation for a tenant takes that tenant's wallet row lock, so quota,
spending-limit and balance checks are serialized per organization and cannot
race. A settlement charges the actual price (provider cost converted to
credits, or the feature's flat price) but never more than was reserved; any
excess is recorded as ``credits_uncollected`` for reconciliation instead of
overdrawing the wallet. The credit ledger records only balance changes
(grants, charges, refunds), never temporary holds.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.usage.exceptions import InsufficientCredits, QuotaExceeded, SpendingLimitReached
from apps.usage.models import Feature, UsageRecord, UsageReservation
from apps.usage.pricing import credits_for_cost, flat_credits, pricing_snapshot, reservation_credits

ZERO = Decimal("0")

# Credit-ledger reason for each feature's usage debits.
_LEDGER_REASONS: dict[str, str] = {
    Feature.CHAT_MESSAGES: "usage_chat",
    Feature.RAG_QUERIES: "usage_rag",
    Feature.SEARCH_QUERIES: "usage_search",
    Feature.KNOWLEDGE_DOCUMENTS: "usage_knowledge",
    Feature.IMAGE_GENERATIONS: "usage_image_gen",
    Feature.DOCUMENT_RENDERS: "usage_doc_render",
    Feature.FILE_CONVERSIONS: "usage_file_conv",
    Feature.WORKFLOW_EXECUTIONS: "usage_workflow",
    Feature.AGENT_RUNS: "usage_agent",
    Feature.ANALYSIS_RUNS: "usage_analysis",
    Feature.API_CALLS: "usage_api",
}


@dataclass(frozen=True)
class UsageCost:
    provider_cost_usd: Decimal = ZERO
    input_tokens: int = 0
    output_tokens: int = 0
    model_run_count: int = 0


def current_period(moment: datetime | None = None) -> str:
    return (moment or timezone.now()).strftime("%Y-%m")


def _locked_wallet(organization: Any) -> Any:
    from apps.billing.services import CreditService

    CreditService.get_or_create_wallet(organization)
    from apps.billing.models import CreditWallet

    return CreditWallet.objects.select_for_update().get(organization=organization)


def active_plan(organization: Any) -> Any:
    """The organization's subscribed plan, else the configured default plan."""
    from apps.billing.models import Plan, Subscription

    subscription = (
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
    if subscription is not None:
        return subscription.plan
    return Plan.objects.filter(slug=settings.BILLING_DEFAULT_PLAN, status=Plan.Status.ACTIVE).first()


def plan_limit(organization: Any, key: str, default: int) -> int:
    plan = active_plan(organization)
    value = (plan.limits or {}).get(key) if plan is not None else None
    try:
        return int(value) if value is not None else default
    except TypeError, ValueError:
        return default


def feature_usage(organization: Any, feature: str, *, include_held: bool = True) -> int:
    """Units of ``feature`` used this period (settled plus currently held)."""
    used = (
        UsageRecord.objects.filter(
            organization=organization, feature=feature, period=current_period()
        ).aggregate(total=Sum("quantity"))["total"]
        or 0
    )
    if include_held:
        used += (
            UsageReservation.objects.filter(
                organization=organization, feature=feature, status=UsageReservation.Status.HELD
            ).aggregate(total=Sum("quantity"))["total"]
            or 0
        )
    return int(used)


def period_spend(organization: Any) -> Decimal:
    """Credits charged this period plus credits currently held."""
    charged = (
        UsageRecord.objects.filter(organization=organization, period=current_period()).aggregate(
            total=Sum("credits_charged")
        )["total"]
        or ZERO
    )
    held = (
        UsageReservation.objects.filter(
            organization=organization, status=UsageReservation.Status.HELD
        ).aggregate(total=Sum("credits_reserved"))["total"]
        or ZERO
    )
    return Decimal(charged) + Decimal(held)


def _check_quota(organization: Any, feature: str, quantity: int) -> None:
    from apps.billing.models import Entitlement

    plan = active_plan(organization)
    if plan is None:
        return
    entitlement = Entitlement.objects.filter(plan=plan, feature=feature).first()
    if entitlement is None or entitlement.limit_type != Entitlement.LimitType.HARD:
        return
    limit = int(entitlement.limit_value or 0)
    used = feature_usage(organization, feature)
    if used + quantity > limit:
        message = f"The {plan.name} plan allows {limit} {feature} per month; {used} are used or in progress."
        raise QuotaExceeded(detail=message)


def _check_spending_limit(organization: Any, wallet: Any, amount: Decimal) -> None:
    limit = wallet.monthly_spending_limit
    if limit is None:
        return
    if period_spend(organization) + amount > limit:
        raise SpendingLimitReached(detail=f"The monthly spending limit of {limit} credits would be exceeded.")


def reserve(
    *,
    organization: Any,
    user: Any,
    feature: str,
    source_type: str,
    source_id: Any,
    quantity: int = 1,
    credits: Decimal | None = None,
) -> UsageReservation:
    """Hold credits for one billable operation (idempotent per source)."""
    if organization is None:
        raise ValueError("An organization is required for metering.")
    amount = Decimal(credits) if credits is not None else reservation_credits(feature) * quantity
    with transaction.atomic():
        wallet = _locked_wallet(organization)
        existing = UsageReservation.objects.filter(source_type=source_type, source_id=str(source_id)).first()
        if existing is not None:
            return existing
        _check_quota(organization, feature, quantity)
        _check_spending_limit(organization, wallet, amount)
        if wallet.available_balance < amount:
            raise InsufficientCredits(
                detail=f"Insufficient credits. Available: {wallet.available_balance}, required: {amount}."
            )
        wallet.reserved_balance += amount
        wallet.save(update_fields=["reserved_balance", "updated_at"])
        return UsageReservation.objects.create(
            organization=organization,
            user=user if getattr(user, "is_authenticated", False) else None,
            feature=feature,
            quantity=quantity,
            credits_reserved=amount,
            source_type=source_type,
            source_id=str(source_id),
            expires_at=timezone.now() + timedelta(minutes=settings.USAGE_RESERVATION_TTL_MINUTES),
        )


def _close(reservation: UsageReservation, wallet: Any, status: str, reason: str) -> None:
    wallet.reserved_balance = max(ZERO, wallet.reserved_balance - reservation.credits_reserved)
    reservation.status = status
    reservation.closed_at = timezone.now()
    reservation.close_reason = reason[:200]
    reservation.save(update_fields=["status", "closed_at", "close_reason"])


def settle(
    reservation_id: Any,
    *,
    cost: UsageCost | None = None,
    quantity: int | None = None,
    credits: Decimal | None = None,
) -> UsageRecord | None:
    """Charge actual usage against a hold (idempotent; never exceeds the hold)."""
    from apps.billing.models import CreditLedger

    with transaction.atomic():
        reservation = (
            UsageReservation.objects.select_for_update()
            .select_related("organization")
            .filter(id=reservation_id)
            .first()
        )
        if reservation is None:
            return None
        if reservation.status != UsageReservation.Status.HELD:
            return UsageRecord.objects.filter(reservation=reservation).first()
        wallet = _locked_wallet(reservation.organization)
        units = quantity if quantity is not None else reservation.quantity
        cost = cost or UsageCost()
        flat = flat_credits(reservation.feature) * units
        if credits is not None:
            # An explicit amount from a trusted, signed integration callback.
            basis = UsageRecord.Basis.FLAT
            due = max(ZERO, Decimal(credits))
        elif cost.provider_cost_usd > 0:
            basis = UsageRecord.Basis.PROVIDER_COST
            due = max(flat, credits_for_cost(cost.provider_cost_usd))
        else:
            basis = UsageRecord.Basis.FLAT
            due = flat
        charged = min(due, reservation.credits_reserved, wallet.balance)
        charged = max(ZERO, charged)
        _close(reservation, wallet, UsageReservation.Status.SETTLED, "settled")
        wallet.balance -= charged
        wallet.save(update_fields=["reserved_balance", "balance", "updated_at"])
        record = UsageRecord.objects.create(
            organization=reservation.organization,
            user_id=reservation.user_id,
            reservation=reservation,
            feature=reservation.feature,
            quantity=units,
            source_type=reservation.source_type,
            source_id=reservation.source_id,
            basis=basis,
            credits_charged=charged,
            credits_uncollected=due - charged,
            provider_cost_usd=cost.provider_cost_usd,
            input_tokens=cost.input_tokens,
            output_tokens=cost.output_tokens,
            model_run_count=cost.model_run_count,
            pricing={**pricing_snapshot(), "due": str(due), "reserved": str(reservation.credits_reserved)},
            period=current_period(),
        )
        if charged > 0:
            CreditLedger.objects.create(
                wallet=wallet,
                direction=CreditLedger.Direction.DEBIT,
                credits=charged,
                reason=_LEDGER_REASONS.get(reservation.feature, "usage_api"),
                description=f"{reservation.get_feature_display()} usage ({reservation.source_type})",
                request_id=reservation.id,
                job_id=reservation.source_id if reservation.source_type == "job" else None,
                idempotency_key=f"usage_{reservation.id}",
                price_snapshot=record.pricing,
                balance_after=wallet.balance,
                metadata={"usageRecordId": str(record.id), "basis": basis},
            )
    return record


def release(reservation_id: Any, *, reason: str = "released", expired: bool = False) -> bool:
    """Return a hold to the wallet without charging (idempotent)."""
    with transaction.atomic():
        reservation = UsageReservation.objects.select_for_update().filter(id=reservation_id).first()
        if reservation is None or reservation.status != UsageReservation.Status.HELD:
            return False
        wallet = _locked_wallet(reservation.organization)
        status = UsageReservation.Status.EXPIRED if expired else UsageReservation.Status.RELEASED
        _close(reservation, wallet, status, reason)
        wallet.save(update_fields=["reserved_balance", "updated_at"])
    return True


def charge_now(
    *, organization: Any, user: Any, feature: str, source_type: str, source_id: Any, quantity: int = 1
) -> UsageRecord | None:
    """Reserve and settle a synchronous, flat-priced operation in one step."""
    reservation = reserve(
        organization=organization,
        user=user,
        feature=feature,
        source_type=source_type,
        source_id=source_id,
        quantity=quantity,
        credits=flat_credits(feature) * quantity,
    )
    return settle(reservation.id, quantity=quantity)


def reservation_for(source_type: str, source_id: Any) -> UsageReservation | None:
    return UsageReservation.objects.filter(source_type=source_type, source_id=str(source_id)).first()


def finalize_source(source_type: str, source_id: Any) -> str:
    """Settle or release the hold of a finished source; ``"pending"`` while it runs."""
    from apps.usage.sources import resolve

    reservation = reservation_for(source_type, source_id)
    if reservation is None or reservation.status != UsageReservation.Status.HELD:
        return "none"
    state = resolve(source_type, source_id)
    if state is None:
        return "unknown"
    if not state.terminal:
        return "pending"
    if state.succeeded:
        settle(reservation.id, cost=state.cost, quantity=state.quantity)
        return "settled"
    release(reservation.id, reason=state.reason or "source did not complete")
    return "released"


class Metered:
    """Handle yielded by :func:`metered`; set ``quantity`` or ``cost`` before exit."""

    def __init__(self, reservation: UsageReservation) -> None:
        self.reservation = reservation
        self.quantity: int | None = None
        self.cost: UsageCost | None = None


@contextmanager
def metered(
    *,
    organization: Any,
    user: Any,
    feature: str,
    source_type: str,
    source_id: Any,
    quantity: int = 1,
    credits: Decimal | None = None,
) -> Iterator[Metered]:
    """Reserve before a synchronous billable operation; settle on success, release on error."""
    reservation = reserve(
        organization=organization,
        user=user,
        feature=feature,
        source_type=source_type,
        source_id=source_id,
        quantity=quantity,
        credits=credits,
    )
    handle = Metered(reservation)
    try:
        yield handle
    except BaseException:
        release(reservation.id, reason="operation failed")
        raise
    if handle.quantity == 0:
        release(reservation.id, reason="nothing was produced")
    else:
        settle(reservation.id, cost=handle.cost, quantity=handle.quantity)
