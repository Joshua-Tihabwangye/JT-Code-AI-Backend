from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.usage.views import (
    InternalReconciliationViewSet,
    InternalReservationViewSet,
    InternalUsageOrganizationsView,
    InternalUsageSummaryView,
    UsageRecordViewSet,
    UsageView,
)

router = DefaultRouter()
router.register(r"usage/records", UsageRecordViewSet, basename="usage-record")
router.register(
    r"internal/usage/reconciliations", InternalReconciliationViewSet, basename="usage-reconciliation"
)
router.register(r"internal/usage/reservations", InternalReservationViewSet, basename="usage-reservation")

urlpatterns = [
    path("usage/", UsageView.as_view(), name="usage"),
    path("internal/usage/summary/", InternalUsageSummaryView.as_view(), name="internal-usage-summary"),
    path(
        "internal/usage/organizations/",
        InternalUsageOrganizationsView.as_view(),
        name="internal-usage-organizations",
    ),
    path("", include(router.urls)),
]
