from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.visualize import dashboard_views, views

router = DefaultRouter()
router.register(r"charts", views.SavedChartViewSet, basename="viz-chart")
router.register(r"dashboards", dashboard_views.DashboardViewSet, basename="viz-dashboard")
router.register(r"themes", dashboard_views.ThemeViewSet, basename="viz-theme")
router.register(r"measures", dashboard_views.MeasureViewSet, basename="viz-measure")

urlpatterns = [
    path("catalog/", views.CatalogView.as_view(), name="viz-catalog"),
    path("samples/", views.SampleDatasetView.as_view(), name="viz-samples"),
    path("datasets/import/", views.DatasetImportView.as_view(), name="viz-dataset-import"),
    path(
        "datasets/<uuid:dataset_id>/preview/", views.DatasetPreviewView.as_view(), name="viz-dataset-preview"
    ),
    path(
        "datasets/<uuid:dataset_id>/insights/",
        views.DatasetInsightsView.as_view(),
        name="viz-dataset-insights",
    ),
    path(
        "datasets/<uuid:dataset_id>/recommend/",
        views.DatasetRecommendView.as_view(),
        name="viz-dataset-recommend",
    ),
    path(
        "datasets/<uuid:dataset_id>/organize/",
        views.DatasetOrganizeView.as_view(),
        name="viz-dataset-organize",
    ),
    path("datasets/<uuid:dataset_id>/export/", views.DatasetExportView.as_view(), name="viz-dataset-export"),
    path(
        "datasets/<uuid:dataset_id>/quality/", views.DatasetQualityView.as_view(), name="viz-dataset-quality"
    ),
    path("datasets/<uuid:dataset_id>/edit/", views.DatasetEditView.as_view(), name="viz-dataset-edit"),
    path(
        "datasets/<uuid:dataset_id>/replace/", views.DatasetReplaceView.as_view(), name="viz-dataset-replace"
    ),
    path("maps/world/", views.WorldMapView.as_view(), name="viz-map-world"),
    path("render/", views.RenderView.as_view(), name="viz-render"),
    path("predict/", views.PredictView.as_view(), name="viz-predict"),
    path("predict/results/<str:result_id>/", views.PredictionResultView.as_view(), name="viz-predict-result"),
    path("workbook/", views.WorkbookView.as_view(), name="viz-workbook"),
    path("visuals/", dashboard_views.VisualsView.as_view(), name="viz-visuals"),
    path("assistant/", views.AssistantView.as_view(), name="viz-assistant"),
    path("", include(router.urls)),
]
