from django.urls import path

from apps.assets.views import (
    AssetAccessView,
    AssetDetailView,
    AssetListView,
    CompleteUploadView,
    ImageKitSignatureView,
)

urlpatterns = [
    path("files/", AssetListView.as_view(), name="asset-list"),
    path("files/signature/", ImageKitSignatureView.as_view(), name="asset-signature"),
    path("files/complete/", CompleteUploadView.as_view(), name="asset-complete"),
    path("files/<uuid:id>/", AssetDetailView.as_view(), name="asset-detail"),
    path("files/<uuid:id>/access/", AssetAccessView.as_view(), name="asset-access"),
]
