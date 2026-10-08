from django.urls import path

from apps.assets.views import (
    AssetAccessView,
    AssetAttachView,
    AssetBulkDeleteView,
    AssetDetailView,
    AssetDownloadView,
    AssetListView,
    AssetRestoreView,
    CompleteUploadView,
    SupabaseStorageUploadView,
)

urlpatterns = [
    path("files/", AssetListView.as_view(), name="asset-list"),
    path("files/signature/", SupabaseStorageUploadView.as_view(), name="asset-signature"),
    path("files/complete/", CompleteUploadView.as_view(), name="asset-complete"),
    path("files/bulk-delete/", AssetBulkDeleteView.as_view(), name="asset-bulk-delete"),
    path("files/<uuid:id>/", AssetDetailView.as_view(), name="asset-detail"),
    path("files/<uuid:id>/access/", AssetAccessView.as_view(), name="asset-access"),
    path("files/<uuid:id>/download/", AssetDownloadView.as_view(), name="asset-download"),
    path("files/<uuid:id>/attach/", AssetAttachView.as_view(), name="asset-attach"),
    path("files/<uuid:id>/restore/", AssetRestoreView.as_view(), name="asset-restore"),
]
