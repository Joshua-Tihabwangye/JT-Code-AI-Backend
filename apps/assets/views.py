from __future__ import annotations

import secrets

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.assets.imagekit import (
    content_checksum,
    generate_signed_delivery_url,
    generate_upload_auth,
    imagekit_is_configured,
    provider_identity_fingerprint,
    sanitize_file_name,
    user_upload_folder,
    verify_imagekit_file,
)
from apps.assets.models import Asset, UploadIntent
from apps.assets.serializers import (
    AssetAccessResponseSerializer,
    AssetSerializer,
    CompleteUploadSerializer,
    SignatureRequestSerializer,
)
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
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = SignatureRequestSerializer

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
        organization = organization_for_request(request, required=True)
        folder = user_upload_folder(request.user)
        expires_at = timezone.now() + timezone.timedelta(seconds=settings.IMAGEKIT_UPLOAD_AUTH_TTL_SECONDS)
        intent = UploadIntent(
            owner=request.user,
            organization=organization,
            token="pending",
            folder=folder,
            file_name="pending",
            original_filename=serializer.validated_data["originalFilename"],
            content_type=serializer.validated_data["contentType"],
            expected_bytes=serializer.validated_data["bytes"],
            expires_at=expires_at,
        )
        intent.file_name = f"{intent.id}-{sanitize_file_name(serializer.validated_data['originalFilename'])}"
        auth = generate_upload_auth(expire=int(expires_at.timestamp()))
        intent.token = auth["token"]
        intent.save()
        return Response(
            {
                "uploadIntentId": str(intent.id),
                "publicKey": settings.IMAGEKIT_PUBLIC_KEY,
                "endpointUrl": settings.IMAGEKIT_ENDPOINT_URL,
                "uploadUrl": "https://upload.imagekit.io/api/v1/files/upload",
                "folder": folder,
                "fileName": intent.file_name,
                "useUniqueFileName": False,
                "overwriteFile": False,
                "isPrivateFile": True,
                "checks": f'"file.size" <= {intent.expected_bytes}',
                "token": auth["token"],
                "expire": auth["expire"],
                "signature": auth["signature"],
            }
        )


class CompleteUploadView(APIView):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = CompleteUploadSerializer

    def post(self, request: Request) -> Response:
        serializer = CompleteUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization = organization_for_request(request, required=True)
        intent = UploadIntent.objects.filter(
            id=serializer.validated_data["uploadIntentId"],
            owner=request.user,
            organization=organization,
        ).first()
        if intent is None:
            return Response({"detail": "Upload intent not found."}, status=status.HTTP_404_NOT_FOUND)
        if not secrets.compare_digest(intent.token, serializer.validated_data["uploadToken"]):
            return Response({"detail": "Upload intent token is invalid."}, status=status.HTTP_403_FORBIDDEN)
        if intent.status == UploadIntent.Status.COMPLETED:
            asset = Asset.objects.filter(imagekit_file_id=intent.imagekit_file_id).first()
            if asset and asset.owner_id == request.user.id:
                return Response(AssetSerializer(asset).data, status=status.HTTP_200_OK)
            return Response({"detail": "Upload intent has already been consumed."}, status=409)
        if intent.status != UploadIntent.Status.PENDING or intent.expires_at <= timezone.now():
            UploadIntent.objects.filter(id=intent.id).update(status=UploadIntent.Status.EXPIRED)
            return Response({"detail": "Upload intent has expired."}, status=status.HTTP_410_GONE)
        file_id = serializer.validated_data["fileId"]
        file_path = serializer.validated_data["filePath"]
        expected_path = f"{intent.folder.rstrip('/')}/{intent.file_name}"
        if file_path != expected_path:
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
        if not all(resource.get(field) for field in ("url", "fileType")):
            return Response(
                {"detail": "ImageKit asset metadata is incomplete."},
                status=status.HTTP_409_CONFLICT,
            )
        if int(resource.get("size", -1)) != intent.expected_bytes:
            return Response({"detail": "ImageKit asset size mismatch."}, status=status.HTTP_409_CONFLICT)
        if resource.get("isPrivateFile") is not True:
            return Response({"detail": "ImageKit asset must be private."}, status=status.HTTP_409_CONFLICT)
        try:
            checksum, delivered_content_type = content_checksum(
                file_path, expected_size=intent.expected_bytes
            )
        except Exception:
            return Response(
                {"detail": "ImageKit asset content could not be verified."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if (
            delivered_content_type
            and delivered_content_type != "application/octet-stream"
            and delivered_content_type != intent.content_type
        ):
            return Response(
                {"detail": "ImageKit asset content type mismatch."},
                status=status.HTTP_409_CONFLICT,
            )
        with transaction.atomic():
            locked_intent = UploadIntent.objects.select_for_update().get(id=intent.id)
            if locked_intent.status != UploadIntent.Status.PENDING:
                asset = Asset.objects.filter(imagekit_file_id=locked_intent.imagekit_file_id).first()
                if asset:
                    return Response(AssetSerializer(asset).data, status=status.HTTP_200_OK)
                return Response({"detail": "Upload intent is no longer available."}, status=409)
            if Asset.objects.filter(imagekit_file_id=file_id).exists():
                return Response(
                    {"detail": "ImageKit file is already registered."},
                    status=status.HTTP_409_CONFLICT,
                )
            version_name = str((resource.get("versionInfo") or {}).get("name") or "")
            version_suffix = version_name.rsplit(" ", 1)[-1]
            asset = Asset.objects.create(
                owner=request.user,
                organization=organization,
                imagekit_file_id=file_id,
                imagekit_file_path=file_path,
                secure_url=resource["url"],
                resource_type=resource["fileType"],
                format=str(resource.get("format") or "")[:50],
                bytes=int(resource["size"]),
                version=int(version_suffix) if version_suffix.isdigit() else 0,
                original_filename=intent.original_filename,
                metadata={
                    "thumbnail_url": resource.get("thumbnailUrl"),
                    "version_info": resource.get("versionInfo"),
                    "content_type": intent.content_type,
                    "delivered_content_type": delivered_content_type,
                },
                checksum_sha256=checksum,
                provider_fingerprint=provider_identity_fingerprint(resource),
                provenance={
                    "provider": "imagekit",
                    "provider_file_id": file_id,
                    "upload_intent_id": str(intent.id),
                    "verified_at": timezone.now().isoformat(),
                    "verified_by": str(request.user.id),
                },
            )
            locked_intent.status = UploadIntent.Status.COMPLETED
            locked_intent.imagekit_file_id = file_id
            locked_intent.completed_at = timezone.now()
            locked_intent.save(update_fields=["status", "imagekit_file_id", "completed_at"])
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
        return Response(AssetSerializer(asset).data, status=status.HTTP_201_CREATED)


class AssetAccessView(APIView):
    """Return a short-lived provider URL only after tenant authorization."""

    permission_classes = [IsAuthenticated]
    serializer_class = AssetAccessResponseSerializer

    def post(self, request: Request, id) -> Response:
        asset = (
            tenant_scoped_queryset(Asset.objects.filter(id=id), request.user)
            .filter(status=Asset.Status.READY)
            .first()
        )
        if asset is None:
            return Response({"detail": "Asset not found."}, status=status.HTTP_404_NOT_FOUND)
        if not imagekit_is_configured():
            return Response(
                {"detail": "ImageKit is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        return Response(
            {
                "assetId": str(asset.id),
                "url": generate_signed_delivery_url(asset.imagekit_file_path),
                "expiresIn": settings.IMAGEKIT_SIGNED_URL_TTL_SECONDS,
            }
        )


class AssetDetailView(APIView):
    """Read or soft-delete a tenant-owned asset; provider deletion is delayed."""

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = AssetSerializer

    def get(self, request: Request, id) -> Response:
        asset = tenant_scoped_queryset(Asset.objects.filter(id=id), request.user).first()
        if asset is None:
            return Response({"detail": "Asset not found."}, status=status.HTTP_404_NOT_FOUND)
        self.check_object_permissions(request, asset)
        return Response(AssetSerializer(asset).data)

    def delete(self, request: Request, id) -> Response:
        asset = (
            tenant_scoped_queryset(Asset.objects.filter(id=id), request.user)
            .exclude(status=Asset.Status.DELETED)
            .first()
        )
        if asset is None:
            return Response({"detail": "Asset not found."}, status=status.HTTP_404_NOT_FOUND)
        self.check_object_permissions(request, asset)
        asset.status = Asset.Status.DELETED
        asset.deleted_at = timezone.now()
        asset.deletion_error = ""
        asset.save(update_fields=["status", "deleted_at", "deletion_error", "updated_at"])
        add_outbox_event(
            "asset.deleted",
            str(asset.id),
            {"assetId": str(asset.id), "fileId": asset.imagekit_file_id, "ownerId": str(asset.owner_id)},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
