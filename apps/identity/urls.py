from django.urls import path

from apps.identity.settings_views import (
    SettingsAccountView,
    SettingsConsentsView,
    SettingsExportView,
    SettingsOrganizationView,
)
from apps.identity.views import AuthPingView, MeView, SettingsProfileView
from apps.identity.webhooks import supabase_webhook

urlpatterns = [
    path('auth/ping/', AuthPingView.as_view(), name='auth-ping'),
    path('me/', MeView.as_view(), name='me'),
    path('settings/profile/', SettingsProfileView.as_view(), name='settings-profile'),
    path('settings/organization/', SettingsOrganizationView.as_view(), name='settings-organization'),
    path('settings/consents/', SettingsConsentsView.as_view(), name='settings-consents'),
    path('settings/export/', SettingsExportView.as_view(), name='settings-export'),
    path('settings/account/', SettingsAccountView.as_view(), name='settings-account'),
    path('webhooks/supabase/', supabase_webhook, name='supabase-webhook'),
]