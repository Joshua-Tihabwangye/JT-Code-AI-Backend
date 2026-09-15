from django.urls import path
from apps.core.views import LiveView, N8nSentryRelayView, ReadyView

urlpatterns = [
    path('health/live/', LiveView.as_view(), name='health-live'),
    path('health/ready/', ReadyView.as_view(), name='health-ready'),
    path('monitoring/n8n-error/', N8nSentryRelayView.as_view(), name='n8n-sentry-relay'),
]
