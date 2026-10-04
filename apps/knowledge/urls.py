from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.knowledge.views import (
    ChunkViewSet,
    CitationViewSet,
    CollectionViewSet,
    DocumentViewSet,
    KnowledgeQueryView,
    RAGEvaluationView,
    RAGEvaluationViewSet,
    RAGQueryView,
    SearchView,
    SourceViewSet,
    SyncRunViewSet,
)

router = DefaultRouter()
router.register(r"collections", CollectionViewSet, basename="collection")
router.register(r"sources", SourceViewSet, basename="source")
router.register(r"documents", DocumentViewSet, basename="knowledge-document")
router.register(r"chunks", ChunkViewSet, basename="chunk")
router.register(r"sync-runs", SyncRunViewSet, basename="sync-run")
router.register(r"citations", CitationViewSet, basename="citation")
router.register(r"rag-evaluations", RAGEvaluationViewSet, basename="rag-evaluation")

# Mounted at /api/v1/knowledge/ so knowledge documents never collide with the
# document-authoring API at /api/v1/documents/.
urlpatterns = [
    path("", include(router.urls)),
    path("search/", SearchView.as_view(), name="knowledge-search"),
    path("query/", KnowledgeQueryView.as_view(), name="knowledge-query"),
    path("rag/query/", RAGQueryView.as_view(), name="rag-query"),
    path("rag/evaluate/", RAGEvaluationView.as_view(), name="rag-evaluate"),
]
