from __future__ import annotations

from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.assets.imagekit import (
    generate_upload_auth,
    imagekit_is_configured,
    sanitize_file_name,
    user_upload_folder,
    verify_imagekit_file,
)
from apps.assets.models import Asset
from apps.assets.serializers import AssetSerializer, CompleteUploadSerializer, SignatureRequestSerializer
from apps.events.outbox import add_outbox_event
from apps.identity.authorization import (
    HasOrganizationWriteAccess,
    organization_for_request,
    tenant_scoped_queryset,
)


class AssetListView(ListAPIView):
    serializer_class = AssetSerializer

    def get_queryset(self):
        return (
            tenant_scoped_queryset(Asset.objects.all(), self.request.user)
            .exclude(status=Asset.Status.DELETED)
            .order_by("-created_at")
        )


class ImageKitSignatureView(APIView):
    permission_classes = [HasOrganizationWriteAccess]

    def post(self, request: Request) -> Response:
        serializer = SignatureRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data["bytes"] > settings.IMAGEKIT_MAX_UPLOAD_BYTES:
            return Response(
                {"detail": "File exceeds the configured upload limit."},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        if not imagekit_is_configured():
            return Response(
                {"detail": "ImageKit is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        folder = user_upload_folder(request.user)
        auth = generate_upload_auth()
        return Response(
            {
                "publicKey": settings.IMAGEKIT_PUBLIC_KEY,
                "endpointUrl": settings.IMAGEKIT_ENDPOINT_URL,
                "uploadUrl": "https://upload.imagekit.io/api/v1/files/upload",
                "folder": folder,
                "fileName": sanitize_file_name(serializer.validated_data["originalFilename"]),
                "token": auth["token"],
                "expire": auth["expire"],
                "signature": auth["signature"],
            }
        )


class CompleteUploadView(APIView):
    permission_classes = [HasOrganizationWriteAccess]

    def post(self, request: Request) -> Response:
        serializer = CompleteUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        file_id = serializer.validated_data["fileId"]
        file_path = serializer.validated_data["filePath"]
        expected_prefix = f"{user_upload_folder(request.user)}/"
        if not file_path.startswith(expected_prefix):
            return Response(
                {"detail": "Uploaded asset is outside the authorized folder."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not imagekit_is_configured():
            return Response(
                {"detail": "ImageKit is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        try:
            resource = verify_imagekit_file(file_id)
        except Exception:
            return Response(
                {"detail": "ImageKit asset could not be verified."}, status=status.HTTP_400_BAD_REQUEST
            )
        if resource.get("filePath") != file_path:
            return Response({"detail": "ImageKit asset path mismatch."}, status=status.HTTP_409_CONFLICT)
        if int(resource.get("size", -1)) != serializer.validated_data["size"]:
            return Response({"detail": "ImageKit asset size mismatch."}, status=status.HTTP_409_CONFLICT)
        try:
            with transaction.atomic():
                asset = Asset.objects.create(
                    owner=request.user,
                    organization=organization_for_request(request, required=True),
                    imagekit_file_id=file_id,
                    imagekit_file_path=file_path,
                    secure_url=resource.get("url") or serializer.validated_data["url"],
                    resource_type=resource.get("fileType") or serializer.validated_data["fileType"],
                    format=resource.get("format") or serializer.validated_data.get("format", ""),
                    bytes=resource.get("size") or serializer.validated_data["size"],
                    version=serializer.validated_data.get("version", 0),
                    original_filename=serializer.validated_data["originalFilename"],
                    metadata={
                        "thumbnail_url": resource.get("thumbnailUrl"),
                        "version_info": resource.get("versionInfo"),
                    },
                )
                add_outbox_event(
                    "asset.created",
                    str(asset.id),
                    {
                        "assetId": str(asset.id),
                        "fileId": file_id,
                        "ownerId": str(request.user.id),
                        "resourceType": asset.resource_type,
                        "bytes": asset.bytes,
                    },
                )
        except IntegrityError:
            asset = Asset.objects.get(imagekit_file_id=file_id, owner=request.user)
        return Response(AssetSerializer(asset).data, status=status.HTTP_201_CREATED)
