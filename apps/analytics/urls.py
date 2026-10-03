from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.analytics.views import (
    AnalysisRunViewSet,
    DatasetGrantViewSet,
    DatasetViewSet,
    VisualizationViewSet,
)

router = DefaultRouter()
router.register(r"analysis/datasets", DatasetViewSet, basename="dataset")
router.register(r"analysis/dataset-grants", DatasetGrantViewSet, basename="dataset-grant")
router.register(r"analysis/runs", AnalysisRunViewSet, basename="analysis-run")
router.register(r"visualizations", VisualizationViewSet, basename="visualization")

urlpatterns = [path("", include(router.urls))]
