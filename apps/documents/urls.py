from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.documents.views import DocumentViewSet, document_download

router = DefaultRouter()
router.register(r'documents', DocumentViewSet, basename='document')

urlpatterns = [
    path('', include(router.urls)),
    path('documents/<uuid:id>/download/', document_download, name='document-download'),
]