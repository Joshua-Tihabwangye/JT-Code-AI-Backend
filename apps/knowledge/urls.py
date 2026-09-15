from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.knowledge.views import (
    CollectionViewSet,
    SourceViewSet,
    DocumentViewSet,
    ChunkViewSet,
    SyncRunViewSet,
    CitationViewSet,
    SearchView,
    RAGQueryView,
)

router = DefaultRouter()
router.register(r'collections', CollectionViewSet, basename='collection')
router.register(r'sources', SourceViewSet, basename='source')
router.register(r'documents', DocumentViewSet, basename='knowledge-document')
router.register(r'chunks', ChunkViewSet, basename='chunk')
router.register(r'sync-runs', SyncRunViewSet, basename='sync-run')
router.register(r'citations', CitationViewSet, basename='citation')

urlpatterns = [
    path('', include(router.urls)),
    path('search/', SearchView.as_view(), name='knowledge-search'),
    path('rag/query/', RAGQueryView.as_view(), name='rag-query'),
]