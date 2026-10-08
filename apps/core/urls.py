from django.urls import path

from apps.core.views import LiveView, ReadyView, StartupView
from apps.orchestration.views import ErrorRelayView

urlpatterns = [
    path("health/live/", LiveView.as_view(), name="health-live"),
    path("health/ready/", ReadyView.as_view(), name="health-ready"),
    path("health/startup/", StartupView.as_view(), name="health-startup"),
    # Pre-Phase-16 path of the n8n error relay; new workflows post to /n8n/errors/.
    path("monitoring/n8n-error/", ErrorRelayView.as_view(), name="n8n-sentry-relay"),
]
