from django.urls import path

from apps.assets.views import AssetListView, CompleteUploadView, ImageKitSignatureView

urlpatterns = [
    path("files/", AssetListView.as_view(), name="asset-list"),
    path("files/signature/", ImageKitSignatureView.as_view(), name="asset-signature"),
    path("files/complete/", CompleteUploadView.as_view(), name="asset-complete"),
]
