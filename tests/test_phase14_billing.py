"""Phase 14 exit proofs: duplicate webhooks and Stripe reconciliation, plus plans,
Checkout, subscriptions, wallet, top-ups, refunds and invoices.

Stripe's HTTP API is replaced at the SDK boundary; webhook payloads are signed
with Stripe's real ``t=…,v1=…`` HMAC-SHA256 scheme and verified by the SDK.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
import stripe
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

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
from apps.billing.webhooks import apply_subscription
from apps.identity.models import Organization, Role, UserOrganization, UserRole

WEBHOOK = "/api/v1/webhooks/stripe/"


def make_org(django_user_model, label: str, *, balance: str = "0"):
    owner = django_user_model.objects.create_user(
        username=f"{label}-owner", supabase_user_id=f"supabase-{label}", email=f"{label}@example.test"
    )
    organization = Organization.objects.create(name=f"{label} org", slug=f"{label}-org", owner=owner)
    UserOrganization.objects.create(user=owner, organization=organization)
    role, _ = Role.objects.get_or_create(name=Role.RoleType.ADMIN)
    UserRole.objects.get_or_create(user=owner, role=role, organization=organization)
    CreditWallet.objects.update_or_create(organization=organization, defaults={"balance": Decimal(balance)})
    customer = BillingCustomer.objects.create(organization=organization, stripe_customer_id=f"cus_{label}")
    return organization, owner, customer


def client_for(user, organization) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    client.credentials(HTTP_X_ORGANIZATION_ID=str(organization.id))
    return client


def signed_post(event: dict, *, secret: str = "whsec_jtcode_unit_tests"):  # pragma: allowlist secret
    payload = json.dumps(event)
    timestamp = int(time.time())
    signature = hmac.new(secret.encode(), f"{timestamp}.{payload}".encode(), hashlib.sha256).hexdigest()
    return APIClient().post(
        WEBHOOK,
        payload,
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE=f"t={timestamp},v1={signature}",
    )


def event(event_id: str, event_type: str, obj: dict, *, created: int | None = None) -> dict:
    return {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "created": created or int(time.time()),
        "livemode": False,
        "data": {"object": obj},
    }


def stripe_subscription(
    sub_id: str, customer: str, price: str, *, status: str = "active", cancel: bool = False
):
    now = int(time.time())
    return {
        "id": sub_id,
        "object": "subscription",
        "customer": customer,
        "status": status,
        "cancel_at_period_end": cancel,
        "current_period_start": now,
        "current_period_end": now + 30 * 86400,
        "canceled_at": None,
        "metadata": {},
        "items": {"data": [{"price": {"id": price, "recurring": {"interval": "month"}}}]},
    }


@pytest.fixture
def pro_plan(db):
    plan = Plan.objects.get(slug="pro")
    plan.stripe_price_monthly_id = "price_pro_month"
    plan.stripe_price_yearly_id = "price_pro_year"
    plan.save(update_fields=["stripe_price_monthly_id", "stripe_price_yearly_id"])
    return plan


def invoice_object(
    invoice_id: str, customer: str, sub_id: str, *, reason: str = "subscription_create"
) -> dict:
    now = int(time.time())
    return {
        "id": invoice_id,
        "object": "invoice",
        "customer": customer,
        "subscription": sub_id,
        "status": "paid",
        "billing_reason": reason,
        "number": f"INV-{invoice_id[-4:]}",
        "amount_due": 2000,
        "amount_paid": 2000,
        "currency": "usd",
        "invoice_pdf": "https://pay.stripe.com/invoice/acct/pdf",
        "hosted_invoice_url": "https://invoice.stripe.com/i/acct",
        "status_transitions": {"paid_at": now},
        "lines": {"data": [{"description": "1 × Pro", "period": {"start": now, "end": now + 30 * 86400}}]},
    }


# --- Plans --------------------------------------------------------------------------


@pytest.mark.django_db
def test_seeded_plan_catalogue_matches_the_frontend_contract(django_user_model):
    organization, owner, _customer = make_org(django_user_model, "plans")
    response = client_for(owner, organization).get("/api/v1/plans/")
    plans = {plan["slug"]: plan for plan in response.json()["results"]}
    assert set(plans) >= {"free", "pro", "team", "business"}
    pro = plans["pro"]
    assert pro["priceCents"] == 2000 and pro["interval"] == "month" and pro["isPopular"] is True
    assert isinstance(pro["features"], list) and pro["monthlyCredits"] == 1500
    free_quotas = {item["feature"]: item for item in plans["free"]["entitlements"]}
    assert free_quotas["chat_messages"]["limitType"] == "hard"


# --- Webhooks: signatures, duplicates, ordering ----------------------------------------


@pytest.mark.django_db
def test_webhook_rejects_bad_signatures_and_stale_timestamps():
    body = event("evt_sig", "invoice.paid", {"id": "in_x"})
    assert signed_post(body, secret="whsec_wrong").status_code == 400  # pragma: allowlist secret
    payload = json.dumps(body)
    stale = int(time.time()) - 3600
    signature = hmac.new(
        b"whsec_jtcode_unit_tests", f"{stale}.{payload}".encode(), hashlib.sha256
    ).hexdigest()
    response = APIClient().post(
        WEBHOOK, payload, content_type="application/json", HTTP_STRIPE_SIGNATURE=f"t={stale},v1={signature}"
    )
    assert response.status_code == 400
    assert not StripeEvent.objects.exists()


@pytest.mark.django_db
def test_duplicate_invoice_webhook_grants_subscription_credits_once(django_user_model, pro_plan, monkeypatch):
    organization, _owner, customer = make_org(django_user_model, "dup-invoice")
    monkeypatch.setattr(
        stripe.Subscription,
        "retrieve",
        lambda sub_id, **kwargs: stripe_subscription(sub_id, customer.stripe_customer_id, "price_pro_month"),
    )
    body = event(
        "evt_invoice_1", "invoice.paid", invoice_object("in_0001", customer.stripe_customer_id, "sub_1")
    )
    first = signed_post(body)
    second = signed_post(body)
    assert first.status_code == 200 and first.json()["duplicate"] is False
    assert second.status_code == 200 and second.json()["duplicate"] is True
    wallet = CreditWallet.objects.get(organization=organization)
    assert wallet.balance == Decimal("1500")
    assert (
        CreditLedger.objects.filter(wallet=wallet, reason=CreditLedger.Reason.SUBSCRIPTION_GRANT).count() == 1
    )
    assert Subscription.objects.get(provider_subscription_id="sub_1").plan == pro_plan
    invoice = Invoice.objects.get(provider_invoice_id="in_0001")
    assert invoice.status == Invoice.Status.PAID and invoice.number == "INV-0001"


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_concurrent_duplicate_deliveries_process_once(django_user_model, monkeypatch):
    organization, _owner, customer = make_org(django_user_model, "dup-race")
    intent = {
        "id": "pi_race",
        "object": "payment_intent",
        "customer": customer.stripe_customer_id,
        "amount": 1000,
        "amount_received": 1000,
        "currency": "usd",
        "metadata": {"organization_id": str(organization.id), "kind": "topup", "credits": "1000"},
    }
    body = event("evt_pi_race", "payment_intent.succeeded", intent)
    barrier = threading.Barrier(5)
    statuses: list[int] = []

    def deliver():
        try:
            barrier.wait()
            statuses.append(signed_post(body).status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=deliver) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert statuses == [200] * 5
    assert CreditWallet.objects.get(organization=organization).balance == Decimal("1000")
    assert StripeEvent.objects.filter(event_id="evt_pi_race").count() == 1
    assert Payment.objects.get(provider_payment_id="pi_race").status == Payment.Status.SUCCEEDED


@pytest.mark.django_db
def test_out_of_order_subscription_events_converge_on_stripe_state(django_user_model, pro_plan, monkeypatch):
    _organization, _owner, customer = make_org(django_user_model, "order")
    current = {"value": stripe_subscription("sub_order", customer.stripe_customer_id, "price_pro_month")}
    monkeypatch.setattr(stripe.Subscription, "retrieve", lambda sub_id, **kwargs: current["value"])
    now = int(time.time())
    # The newer "canceling" update arrives first; Stripe's current state says cancel_at_period_end.
    current["value"] = stripe_subscription(
        "sub_order", customer.stripe_customer_id, "price_pro_month", cancel=True
    )
    signed_post(event("evt_new", "customer.subscription.updated", {"id": "sub_order"}, created=now))
    # A delayed, older "created" event must not undo it.
    signed_post(event("evt_old", "customer.subscription.created", {"id": "sub_order"}, created=now - 600))
    subscription = Subscription.objects.get(provider_subscription_id="sub_order")
    assert subscription.cancel_at_period_end is True
    stale = stripe_subscription(
        "sub_order", customer.stripe_customer_id, "price_pro_month", status="incomplete"
    )
    apply_subscription(stale, event_created=timezone.now() - timezone.timedelta(hours=1))
    subscription.refresh_from_db()
    assert subscription.status == Subscription.Status.ACTIVE  # stale snapshot ignored


@pytest.mark.django_db
def test_unresolvable_event_fails_then_succeeds_on_retry(django_user_model, pro_plan, monkeypatch):
    from apps.billing.tasks import retry_failed_stripe_events

    _organization, _owner, customer = make_org(django_user_model, "retry")
    monkeypatch.setattr(
        stripe.Subscription,
        "retrieve",
        lambda sub_id, **kwargs: stripe_subscription(sub_id, customer.stripe_customer_id, "price_unknown"),
    )
    response = signed_post(event("evt_unknown_price", "customer.subscription.created", {"id": "sub_retry"}))
    assert response.status_code == 500
    row = StripeEvent.objects.get(event_id="evt_unknown_price")
    assert row.status == StripeEvent.Status.FAILED and "not linked to a plan" in row.last_error
    Plan.objects.filter(id=pro_plan.id).update(stripe_price_monthly_id="price_unknown")
    assert retry_failed_stripe_events() == {"processed": 1, "failed": 0}
    assert Subscription.objects.filter(provider_subscription_id="sub_retry").exists()


@pytest.mark.django_db
def test_yearly_invoices_grant_twelve_months_and_prorations_grant_nothing(
    django_user_model, pro_plan, monkeypatch
):
    organization, _owner, customer = make_org(django_user_model, "yearly")
    yearly = stripe_subscription("sub_year", customer.stripe_customer_id, "price_pro_year")
    yearly["items"]["data"][0]["price"]["recurring"]["interval"] = "year"
    monkeypatch.setattr(stripe.Subscription, "retrieve", lambda sub_id, **kwargs: yearly)
    signed_post(
        event("evt_year", "invoice.paid", invoice_object("in_year", customer.stripe_customer_id, "sub_year"))
    )
    signed_post(
        event(
            "evt_prorate",
            "invoice.paid",
            invoice_object(
                "in_prorate", customer.stripe_customer_id, "sub_year", reason="subscription_update"
            ),
        )
    )
    assert CreditWallet.objects.get(organization=organization).balance == Decimal("18000")


# --- Top-ups and refunds --------------------------------------------------------------


@pytest.mark.django_db
def test_topup_refund_reverses_proportional_credits_without_going_negative(django_user_model):
    organization, _owner, customer = make_org(django_user_model, "refund")
    intent = {
        "id": "pi_refund",
        "customer": customer.stripe_customer_id,
        "amount": 1000,
        "amount_received": 1000,
        "currency": "usd",
        "metadata": {"organization_id": str(organization.id), "kind": "topup", "credits": "1000"},
    }
    signed_post(event("evt_topup", "payment_intent.succeeded", intent))
    charge = {"id": "ch_1", "payment_intent": "pi_refund", "amount_refunded": 500}
    signed_post(event("evt_refund_half", "charge.refunded", charge))
    assert CreditWallet.objects.get(organization=organization).balance == Decimal("500")
    CreditWallet.objects.filter(organization=organization).update(
        balance=Decimal("100")
    )  # credits were spent
    signed_post(event("evt_refund_full", "charge.refunded", {**charge, "amount_refunded": 1000}))
    wallet = CreditWallet.objects.get(organization=organization)
    payment = Payment.objects.get(provider_payment_id="pi_refund")
    assert wallet.balance == 0
    assert payment.status == Payment.Status.REFUNDED and payment.credits_reversed == Decimal("600")
    assert payment.metadata["unreversedCredits"] == "400.000000"


@pytest.mark.django_db
def test_topup_endpoint_enforces_bounds_and_snapshots_credits(django_user_model, monkeypatch, settings):
    settings.BILLING_CREDIT_VALUE_USD = 0.01
    organization, owner, _customer = make_org(django_user_model, "topup")
    created = {}

    def fake_create(**kwargs):
        created.update(kwargs)
        return SimpleNamespace(id="pi_new", client_secret="secret_123")

    monkeypatch.setattr(stripe.PaymentIntent, "create", fake_create)
    api = client_for(owner, organization)
    assert api.post("/api/v1/wallets/me/topup/", {"amount_cents": 100}, format="json").status_code == 400
    response = api.post("/api/v1/wallets/me/topup/", {"amount_cents": 2500}, format="json")
    assert response.status_code == 200, response.content
    assert response.json()["clientSecret"] == "secret_123"
    assert created["metadata"]["credits"] == "2500.000000" and created["metadata"]["kind"] == "topup"
    assert Payment.objects.get(provider_payment_id="pi_new").status == Payment.Status.PENDING


# --- Checkout, subscriptions, wallet, payment methods, invoices --------------------------


@pytest.mark.django_db
def test_checkout_uses_stripe_price_and_rejects_open_redirects(django_user_model, pro_plan, monkeypatch):
    organization, owner, _customer = make_org(django_user_model, "checkout")
    sessions = []

    def fake_session(**kwargs):
        sessions.append(kwargs)
        return SimpleNamespace(id="cs_1", url="https://checkout.stripe.com/c/cs_1")

    monkeypatch.setattr(stripe.checkout.Session, "create", fake_session)
    api = client_for(owner, organization)
    response = api.post("/api/v1/plans/pro/subscribe/", {"interval": "year"}, format="json")
    assert response.status_code == 200 and response.json()["checkoutUrl"].startswith(
        "https://checkout.stripe.com/"
    )
    assert sessions[0]["line_items"] == [{"price": "price_pro_year", "quantity": 1}]
    assert sessions[0]["success_url"].startswith("https://app.example.test/")
    evil = api.post(
        "/api/v1/plans/pro/subscribe/",
        {"interval": "month", "successUrl": "https://evil.example/x"},
        format="json",
    )
    assert evil.status_code == 400
    assert api.post("/api/v1/plans/free/subscribe/", {}, format="json").status_code == 400
    Plan.objects.filter(slug="team").update(stripe_price_monthly_id="")
    assert api.post("/api/v1/plans/team/subscribe/", {}, format="json").status_code == 503


@pytest.mark.django_db
def test_cancel_and_reactivate_go_through_stripe(django_user_model, pro_plan, monkeypatch):
    organization, owner, customer = make_org(django_user_model, "cancel")
    apply_subscription(stripe_subscription("sub_cancel", customer.stripe_customer_id, "price_pro_month"))
    monkeypatch.setattr(
        stripe.Subscription,
        "modify",
        lambda sub_id, cancel_at_period_end, **kwargs: stripe_subscription(
            sub_id, customer.stripe_customer_id, "price_pro_month", cancel=cancel_at_period_end
        ),
    )
    api = client_for(owner, organization)
    canceled = api.post("/api/v1/subscriptions/cancel/")
    assert canceled.status_code == 200 and canceled.json()["cancelAtPeriodEnd"] is True
    assert api.post("/api/v1/subscriptions/cancel/").status_code == 400
    reactivated = api.post("/api/v1/subscriptions/reactivate/")
    assert reactivated.status_code == 200 and reactivated.json()["cancelAtPeriodEnd"] is False
    listing = api.get("/api/v1/subscriptions/").json()["results"][0]
    assert listing["plan"] == "pro" and listing["isActive"] is True and listing["daysRemaining"] >= 29


@pytest.mark.django_db
def test_wallet_settings_and_payment_methods(django_user_model, monkeypatch):
    organization, owner, customer = make_org(django_user_model, "wallet")
    api = client_for(owner, organization)
    assert api.patch("/api/v1/wallets/me/", {"auto_topup_enabled": True}, format="json").status_code == 400
    updated = api.patch(
        "/api/v1/wallets/me/", {"monthly_spending_limit": "250", "auto_topup_threshold": "50"}, format="json"
    )
    assert updated.status_code == 200
    assert updated.json()["monthlySpendingLimit"] == 250.0 and updated.json()["autoTopupThreshold"] == 50.0
    assert (
        api.post("/api/v1/payment-methods/", {"cardNumber": "4242424242424242"}, format="json").status_code
        == 400
    )
    monkeypatch.setattr(
        stripe.SetupIntent,
        "create",
        lambda **kwargs: SimpleNamespace(id="seti_1", client_secret="seti_secret"),
    )
    setup = api.post("/api/v1/payment-methods/", {}, format="json")
    assert setup.status_code == 200 and setup.json()["clientSecret"] == "seti_secret"
    assert api.get("/api/v1/payment-methods/me/").json() is None

    monkeypatch.setattr(stripe.Customer, "modify", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        stripe.PaymentMethod,
        "retrieve",
        lambda pm_id: SimpleNamespace(
            id=pm_id,
            type="card",
            card=SimpleNamespace(brand="visa", last4="4242", exp_month=12, exp_year=2030),
            billing_details=SimpleNamespace(name="Ada"),
        ),
    )
    signed_post(
        event(
            "evt_setup",
            "setup_intent.succeeded",
            {"id": "seti_1", "customer": customer.stripe_customer_id, "payment_method": "pm_1"},
        )
    )
    summary = api.get("/api/v1/payment-methods/me/").json()
    assert summary["last4"] == "4242" and summary["brand"] == "visa"
    assert api.patch("/api/v1/wallets/me/", {"auto_topup_enabled": True}, format="json").status_code == 200


@pytest.mark.django_db
def test_invoices_list_as_array_and_download_only_from_stripe_hosts(django_user_model):
    organization, owner, _customer = make_org(django_user_model, "invoices")
    invoice = Invoice.objects.create(
        organization=organization,
        provider_invoice_id="in_local",
        number="INV-1",
        status=Invoice.Status.PAID,
        amount_cents=2000,
        period_start=timezone.now(),
        period_end=timezone.now(),
        invoice_pdf_url="https://evil.example/pdf",
    )
    api = client_for(owner, organization)
    listing = api.get("/api/v1/invoices/").json()
    assert isinstance(listing, list) and listing[0]["number"] == "INV-1" and listing[0]["amountCents"] == 2000
    assert api.get(f"/api/v1/invoices/{invoice.id}/download/").status_code == 404


# --- Reconciliation ---------------------------------------------------------------------


@pytest.mark.django_db
def test_reconciliation_applies_stripe_state_and_recovers_missed_events(
    django_user_model, pro_plan, monkeypatch
):
    from apps.billing.tasks import catch_up_events, reconcile_pending_payments, reconcile_subscriptions

    organization, _owner, customer = make_org(django_user_model, "reconcile")
    apply_subscription(stripe_subscription("sub_rec", customer.stripe_customer_id, "price_pro_month"))
    canceled = stripe_subscription(
        "sub_rec", customer.stripe_customer_id, "price_pro_month", status="canceled"
    )
    monkeypatch.setattr(
        stripe.Subscription,
        "list",
        lambda **kwargs: SimpleNamespace(auto_paging_iter=lambda: iter([canceled])),
    )
    assert reconcile_subscriptions() >= 1
    assert Subscription.objects.get(provider_subscription_id="sub_rec").status == Subscription.Status.CANCELED

    missed = event(
        "evt_missed",
        "payment_intent.succeeded",
        {
            "id": "pi_missed",
            "customer": customer.stripe_customer_id,
            "amount": 500,
            "amount_received": 500,
            "currency": "usd",
            "metadata": {"organization_id": str(organization.id), "kind": "topup", "credits": "500"},
        },
    )
    wrapped = SimpleNamespace(id="evt_missed", to_dict_recursive=lambda: missed)
    monkeypatch.setattr(
        stripe.Event, "list", lambda **kwargs: SimpleNamespace(auto_paging_iter=lambda: iter([wrapped]))
    )
    assert catch_up_events() == 1
    assert catch_up_events() == 0  # already recorded
    assert CreditWallet.objects.get(organization=organization).balance == Decimal("500")

    Payment.objects.create(
        organization=organization,
        provider_payment_id="pi_pending",
        type=Payment.Type.TOPUP,
        amount_cents=300,
        idempotency_key="topup_pi_pending",
        metadata={"credits": "300"},
    )
    Payment.objects.filter(provider_payment_id="pi_pending").update(
        created_at=timezone.now() - timezone.timedelta(hours=2)
    )
    pending_intent = {
        "id": "pi_pending",
        "status": "succeeded",
        "customer": customer.stripe_customer_id,
        "amount": 300,
        "amount_received": 300,
        "currency": "usd",
        "metadata": {"organization_id": str(organization.id), "kind": "topup", "credits": "300"},
    }
    monkeypatch.setattr(
        stripe.PaymentIntent,
        "retrieve",
        lambda pi_id: SimpleNamespace(to_dict_recursive=lambda: pending_intent),
    )
    assert reconcile_pending_payments() == 1
    assert CreditWallet.objects.get(organization=organization).balance == Decimal("800")


@pytest.mark.django_db
def test_auto_topup_charges_saved_method_once_per_window(django_user_model, monkeypatch):
    from apps.billing.tasks import run_auto_topups

    organization, _owner, customer = make_org(django_user_model, "auto", balance="10")
    customer.default_payment_method_id = "pm_saved"
    customer.save(update_fields=["default_payment_method_id"])
    CreditWallet.objects.filter(organization=organization).update(
        auto_topup_enabled=True,
        auto_topup_threshold=Decimal("50"),
        auto_topup_amount=Decimal("1000"),
        credit_value_usd=Decimal("0.01"),
    )
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id=f"pi_auto_{len(calls)}", client_secret="x")

    monkeypatch.setattr(stripe.PaymentIntent, "create", fake_create)
    assert run_auto_topups() == {"charged": 1, "failed": 0}
    assert calls[0]["off_session"] is True and calls[0]["payment_method"] == "pm_saved"
    assert calls[0]["metadata"]["kind"] == "auto_topup" and calls[0]["amount"] == 1000
    assert run_auto_topups() == {"charged": 0, "failed": 0}  # cooldown
