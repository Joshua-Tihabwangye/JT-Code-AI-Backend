from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.visualize import ai_views, dashboard_views, share_views, views

router = DefaultRouter()
router.register(r"charts", views.SavedChartViewSet, basename="viz-chart")
router.register(r"prediction-presets", views.PredictionPresetViewSet, basename="viz-prediction-preset")
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
    path("ai/page/", ai_views.AIPageView.as_view(), name="viz-ai-page"),
    path("ai/ask/", ai_views.AIAskView.as_view(), name="viz-ai-ask"),
    path("ai/format/", ai_views.AIFormatView.as_view(), name="viz-ai-format"),
    path("ai/suggest/", ai_views.AISuggestView.as_view(), name="viz-ai-suggest"),
    path("explain/", ai_views.ExplainView.as_view(), name="viz-explain"),
    path("shared/<str:token>/", share_views.SharedDashboardView.as_view(), name="viz-shared"),
    path("shared/<str:token>/visuals/", share_views.SharedVisualsView.as_view(), name="viz-shared-visuals"),
    path("", include(router.urls)),
]
