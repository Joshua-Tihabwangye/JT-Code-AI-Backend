from django.urls import path
from apps.assets.views import AssetListView, CloudinarySignatureView, CompleteUploadView

urlpatterns = [
    path('files/', AssetListView.as_view(), name='asset-list'),
    path('files/signature/', CloudinarySignatureView.as_view(), name='asset-signature'),
    path('files/complete/', CompleteUploadView.as_view(), name='asset-complete'),
]
