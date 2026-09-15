from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/v1/', include('apps.core.urls')),
    path('api/v1/', include('apps.identity.urls')),
    path('api/v1/', include('apps.conversations.urls')),
    path('api/v1/', include('apps.assets.urls')),
    path('api/v1/', include('apps.documents.urls')),
    path('api/v1/', include('apps.conversions.urls')),
    path('api/v1/', include('apps.jobs.urls')),
    path('api/v1/', include('apps.knowledge.urls')),
    path('api/v1/', include('apps.billing.urls')),
    path('api/v1/', include('apps.governance.urls')),
    path('api/v1/', include('apps.integrations.urls')),
    path('api/v1/', include('apps.ai_gateway.urls')),
]