from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.conversions.views import ConversionViewSet, conversion_download

router = DefaultRouter()
router.register(r'conversions', ConversionViewSet, basename='conversion')

urlpatterns = [
    path('', include(router.urls)),
    path('conversions/<uuid:id>/download/', conversion_download, name='conversion-download'),
]
