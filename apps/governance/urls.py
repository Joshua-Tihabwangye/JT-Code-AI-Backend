from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.governance.views import (
    AuditEventViewSet,
    ConsentRecordViewSet,
    RetentionRuleViewSet,
    SafetyEventViewSet,
    SupportCaseViewSet,
    GovernanceDashboardView,
)

router = DefaultRouter()
router.register(r'audit-events', AuditEventViewSet, basename='audit-event')
router.register(r'consents', ConsentRecordViewSet, basename='consent')
router.register(r'retention-rules', RetentionRuleViewSet, basename='retention-rule')
router.register(r'safety-events', SafetyEventViewSet, basename='safety-event')
router.register(r'support-cases', SupportCaseViewSet, basename='support-case')

urlpatterns = [
    path('', include(router.urls)),
    path('dashboard/', GovernanceDashboardView.as_view(), name='governance-dashboard'),
]