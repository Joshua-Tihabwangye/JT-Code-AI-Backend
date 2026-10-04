from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.billing.views import (
    BillingPortalView,
    CreditLedgerViewSet,
    InvoiceViewSet,
    PaymentMethodView,
    PaymentViewSet,
    PlanViewSet,
    StripeWebhookView,
    SubscriptionViewSet,
    WalletTopUpView,
    WalletView,
)

router = DefaultRouter()
router.register(r"plans", PlanViewSet, basename="plan")
router.register(r"subscriptions", SubscriptionViewSet, basename="subscription")
router.register(r"ledger", CreditLedgerViewSet, basename="ledger")
router.register(r"invoices", InvoiceViewSet, basename="invoice")
router.register(r"payments", PaymentViewSet, basename="payment")

urlpatterns = [
    path("wallets/me/", WalletView.as_view(), name="wallet-me"),
    path("wallets/me/topup/", WalletTopUpView.as_view(), name="wallet-topup"),
    path("payment-methods/", PaymentMethodView.as_view(), name="payment-methods"),
    path("payment-methods/me/", PaymentMethodView.as_view(), name="payment-method-me"),
    path("billing/portal/", BillingPortalView.as_view(), name="billing-portal"),
    path("webhooks/stripe/", StripeWebhookView.as_view(), name="stripe-webhook"),
    path("", include(router.urls)),
]
