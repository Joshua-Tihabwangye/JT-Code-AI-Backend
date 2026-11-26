"""P17-6: the deploy syncs Stripe plan prices by itself once a Stripe key is set.

Stripe's API is replaced at the SDK boundary (no network).
"""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest
import stripe
from django.core.management import call_command

from apps.billing.models import Plan

ROOT_ENTRYPOINT = "docker/entrypoint.sh"


class FakeStripe:
    def __init__(self, known_products=()):
        self.known_products = set(known_products)
        self.created_products: list[str] = []
        self.created_prices: list[dict] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(stripe.Product, "retrieve", self.retrieve)
        monkeypatch.setattr(stripe.Product, "create", self.create_product)
        monkeypatch.setattr(stripe.Price, "list", lambda **kw: SimpleNamespace(data=[]))
        monkeypatch.setattr(stripe.Price, "create", self.create_price)

    def retrieve(self, product_id):
        if product_id not in self.known_products:
            raise stripe.InvalidRequestError("No such product", "id", code="resource_missing")
        return SimpleNamespace(id=product_id)

    def create_product(self, **kwargs):
        product_id = f"prod_new_{len(self.created_products)}"
        self.created_products.append(kwargs["metadata"]["plan_slug"])
        return SimpleNamespace(id=product_id)

    def create_price(self, **kwargs):
        self.created_prices.append(kwargs)
        return SimpleNamespace(id=f"price_{kwargs['lookup_key']}")


def paid_plans():
    return [plan for plan in Plan.objects.filter(status=Plan.Status.ACTIVE) if not plan.is_free]


@pytest.mark.django_db
def test_without_a_stripe_key_the_deploy_step_does_nothing(settings, monkeypatch):
    settings.STRIPE_SECRET_KEY = ""
    fake = FakeStripe()
    fake.install(monkeypatch)
    out = StringIO()
    call_command("sync_stripe_prices", "--if-configured", stdout=out)
    assert "not configured" in out.getvalue()
    assert fake.created_products == [] and fake.created_prices == []


@pytest.mark.django_db
def test_without_the_flag_a_missing_key_is_still_an_error(settings):
    settings.STRIPE_SECRET_KEY = ""
    assert paid_plans(), "the seeded catalogue has paid plans"
    with pytest.raises(Exception, match="not configured"):
        call_command("sync_stripe_prices", stdout=StringIO())


@pytest.mark.django_db
def test_with_a_key_every_paid_plan_gets_its_prices(settings, monkeypatch):
    settings.STRIPE_SECRET_KEY = "sk_test_unit_only"  # pragma: allowlist secret
    Plan.objects.update(stripe_product_id="", stripe_price_monthly_id="", stripe_price_yearly_id="")
    fake = FakeStripe()
    fake.install(monkeypatch)
    call_command("sync_stripe_prices", "--if-configured", stdout=StringIO())
    for plan in paid_plans():
        plan.refresh_from_db()
        assert plan.stripe_product_id.startswith("prod_new_"), plan.slug
        if plan.price_cents:
            assert plan.stripe_price_for("month") == f"price_jt-code-{plan.slug}-month"
        if plan.price_yearly_cents:
            assert plan.stripe_price_for("year") == f"price_jt-code-{plan.slug}-year"


@pytest.mark.django_db
def test_a_product_from_the_other_stripe_mode_is_replaced_and_a_known_one_kept(settings, monkeypatch):
    settings.STRIPE_SECRET_KEY = "sk_live_unit_only"  # pragma: allowlist secret
    plans = paid_plans()
    assert len(plans) >= 2
    kept, stale = plans[0], plans[1]
    Plan.objects.filter(id=kept.id).update(stripe_product_id="prod_live_known")
    Plan.objects.filter(id=stale.id).update(stripe_product_id="prod_from_test_mode")
    fake = FakeStripe(known_products={"prod_live_known"})
    fake.install(monkeypatch)
    call_command("sync_stripe_prices", stdout=StringIO())
    kept.refresh_from_db()
    stale.refresh_from_db()
    assert kept.stripe_product_id == "prod_live_known"
    assert stale.stripe_product_id.startswith("prod_new_")
    assert kept.slug not in fake.created_products and stale.slug in fake.created_products


@pytest.mark.django_db
def test_other_stripe_errors_are_not_mistaken_for_a_missing_product(settings, monkeypatch):
    settings.STRIPE_SECRET_KEY = "sk_live_unit_only"  # pragma: allowlist secret
    plan = paid_plans()[0]
    Plan.objects.filter(id=plan.id).update(stripe_product_id="prod_x")
    fake = FakeStripe()
    fake.install(monkeypatch)

    def denied(product_id):
        raise stripe.InvalidRequestError("Forbidden", "id", code="permission_denied")

    monkeypatch.setattr(stripe.Product, "retrieve", denied)
    with pytest.raises(stripe.InvalidRequestError):
        call_command("sync_stripe_prices", stdout=StringIO())
    assert fake.created_products == []


def test_the_migrate_role_syncs_prices_without_blocking_the_deploy():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / ROOT_ENTRYPOINT).read_text()
    migrate = text.split("  migrate)")[1].split(";;")[0]
    assert "python manage.py sync_stripe_prices --if-configured \\\n      || echo" in migrate
