from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.billing.views import (
    PlanViewSet,
    SubscriptionViewSet,
    CreditWalletViewSet,
    CreditLedgerViewSet,
    InvoiceViewSet,
    PaymentViewSet,
    StripeWebhookView,
    UsageView,
)

router = DefaultRouter()
router.register(r'plans', PlanViewSet, basename='plan')
router.register(r'subscriptions', SubscriptionViewSet, basename='subscription')
router.register(r'wallets', CreditWalletViewSet, basename='wallet')
router.register(r'ledger', CreditLedgerViewSet, basename='ledger')
router.register(r'invoices', InvoiceViewSet, basename='invoice')
router.register(r'payments', PaymentViewSet, basename='payment')

urlpatterns = [
    path('', include(router.urls)),
    path('webhooks/stripe/', StripeWebhookView.as_view(), name='stripe-webhook'),
    path('usage/', UsageView.as_view(), name='usage'),
]