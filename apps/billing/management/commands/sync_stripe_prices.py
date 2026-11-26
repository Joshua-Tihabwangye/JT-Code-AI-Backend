"""Create or link the Stripe Product and Prices for every active paid plan.

Prices are looked up by ``lookup_key`` (``jt-code-<slug>-<interval>``) before
anything is created, so the command is safe to re-run. When a plan's price
changes, a new Stripe Price is created and the lookup key transferred to it
(Stripe Prices are immutable); existing subscriptions keep their old price.

    python manage.py sync_stripe_prices [--dry-run] [--if-configured]

The deploy's migrate step runs it with ``--if-configured``: without a Stripe key it
does nothing, so adding the keys is all an operator does. A stored Product id from
the other Stripe mode (test vs live) is replaced, not reused.
"""

from __future__ import annotations

from typing import Any

import stripe
from django.core.management.base import BaseCommand

from apps.billing.models import Plan
from apps.billing.stripe_client import _call, configure, stripe_configured


class Command(BaseCommand):
    help = "Create/link Stripe Products and Prices for the plan catalogue."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--if-configured",
            action="store_true",
            help="Do nothing (exit 0) when STRIPE_SECRET_KEY is not set.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["if_configured"] and not stripe_configured():
            self.stdout.write("Stripe is not configured; plan prices not synced.")
            return
        for plan in Plan.objects.filter(status=Plan.Status.ACTIVE).order_by("sort_order"):
            if plan.is_free:
                continue
            if options["dry_run"]:
                self.stdout.write(
                    f"would sync {plan.slug}: {plan.price_cents}/month, {plan.price_yearly_cents}/year"
                )
                continue
            product_id = self._existing_product(plan.stripe_product_id) or self._product(plan)
            updates: dict[str, str] = {"stripe_product_id": product_id}
            for interval, amount, field in (
                ("month", plan.price_cents, "stripe_price_monthly_id"),
                ("year", plan.price_yearly_cents, "stripe_price_yearly_id"),
            ):
                if amount > 0:
                    updates[field] = self._price(plan, product_id, interval, amount)
            Plan.objects.filter(id=plan.id).update(**updates)
            self.stdout.write(self.style.SUCCESS(f"{plan.slug}: {updates}"))

    def _existing_product(self, product_id: str) -> str:
        """The stored Product id if this Stripe account (and mode) has it, else ""."""
        if not product_id:
            return ""
        configure()
        try:
            stripe.Product.retrieve(product_id)
        except stripe.InvalidRequestError as exc:
            if getattr(exc, "code", None) != "resource_missing":
                raise
            self.stdout.write(f"stored product {product_id} not found in this Stripe account; creating one")
            return ""
        return product_id

    def _product(self, plan: Plan) -> str:
        product = _call(
            stripe.Product.create,
            name=f"JT-Code {plan.name}",
            description=plan.description or None,
            metadata={"plan_slug": plan.slug},
            idempotency_key=f"jt-product-{plan.slug}",
        )
        return str(product.id)

    def _price(self, plan: Plan, product_id: str, interval: str, amount: int) -> str:
        lookup_key = f"jt-code-{plan.slug}-{interval}"
        existing = _call(stripe.Price.list, lookup_keys=[lookup_key], active=True, limit=1).data
        if existing and existing[0].unit_amount == amount and existing[0].currency == plan.currency.lower():
            return str(existing[0].id)
        price = _call(
            stripe.Price.create,
            product=product_id,
            unit_amount=amount,
            currency=plan.currency.lower(),
            recurring={"interval": interval},
            lookup_key=lookup_key,
            transfer_lookup_key=True,
            metadata={"plan_slug": plan.slug},
        )
        return str(price.id)
