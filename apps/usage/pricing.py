"""Credit pricing: configured per-feature prices and provider-cost conversion."""

from __future__ import annotations

from decimal import ROUND_UP, Decimal

from django.conf import settings

_QUANTUM = Decimal("0.000001")


def _table(raw: str) -> dict[str, Decimal]:
    table: dict[str, Decimal] = {}
    for item in (raw or "").split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            table[key.strip()] = Decimal(value.strip())
    return table


def reservation_credits(feature: str) -> Decimal:
    """Default reservation ceiling for one unit of ``feature``."""
    return _table(settings.USAGE_RESERVATION_CREDITS).get(feature, Decimal("10"))


def flat_credits(feature: str) -> Decimal:
    """Flat price (or minimum charge for AI-metered features) for one unit."""
    return _table(settings.USAGE_FLAT_CREDITS).get(feature, Decimal("1"))


def credits_for_cost(cost_usd: Decimal) -> Decimal:
    """Provider cost in USD → credits, with FX buffer and margin, rounded up."""
    value = (
        Decimal(cost_usd)
        * Decimal(str(settings.BILLING_FX_BUFFER))
        * Decimal(str(settings.BILLING_MARGIN_MULTIPLIER))
        / Decimal(str(settings.BILLING_CREDIT_VALUE_USD))
    )
    return value.quantize(_QUANTUM, rounding=ROUND_UP)


def pricing_snapshot() -> dict[str, str]:
    return {
        "creditValueUsd": str(settings.BILLING_CREDIT_VALUE_USD),
        "fxBuffer": str(settings.BILLING_FX_BUFFER),
        "margin": str(settings.BILLING_MARGIN_MULTIPLIER),
    }
