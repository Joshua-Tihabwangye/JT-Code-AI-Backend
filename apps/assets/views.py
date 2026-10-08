"""Asset API (``/api/v1/files/``) over private Supabase Storage.

Reads are filtered by :mod:`apps.assets.access` (private assets: owner and
organization admins; organization assets: every member). Changing or deleting
an asset requires its owner or an admin; uploading requires editor access.
"""

from __future__ import annotations

import secrets
import uuid

from django.conf import settings
from django.db import transaction
from django.http import StreamingHttpResponse
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.assets.access import assets_visible_to, can_manage_asset
from apps.assets.models import Asset, ConversationAttachment, UploadIntent
from apps.assets.serializers import (
    AssetAccessResponseSerializer,
    AssetSerializer,
    AssetUpdateSerializer,
    AssetUploadSerializer,
    AttachSerializer,
    BulkDeleteSerializer,
    CompleteUploadSerializer,
    SignatureRequestSerializer,
)
from apps.assets.services import asset_references, register_uploaded_asset, restore_asset, soft_delete_asset
from apps.assets.supabase_storage import (
    FINGERPRINT_VERSION,
    SupabaseStorageError,
    content_checksum,
    content_matches_type,
    create_signed_upload,
    generate_signed_delivery_url,
    provider_identity_fingerprint,
    sanitize_file_name,
    stream_file,
    supabase_storage_is_configured,
    user_upload_folder,
    validate_upload_type,
)
from apps.core.throttling import BurstThrottle
from apps.events.outbox import add_outbox_event
from apps.identity.authorization import (
    HasOrganizationWriteAccess,
    organization_for_request,
    require_organization_write_access,
    tenant_scoped_queryset,
)

_LIST_LIMIT = 500


def _unavailable() -> Response:
    return Response(
        {"detail": "Supabase Storage is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE
    )


def _serialize(assets: list[Asset]) -> list[dict]:
    references = asset_references([asset.id for asset in assets])
    return list(AssetSerializer(assets, many=True, context={"references": references}).data)


def _visible_asset(request: Request, asset_id, *, include_deleted: bool = False) -> Asset:
    queryset = assets_visible_to(request.user).filter(id=asset_id)
    if not include_deleted:
        queryset = queryset.exclude(status=Asset.Status.DELETED)
    asset = queryset.first()
    if asset is None:
        raise NotFound("Asset not found.")
    return asset


def _require_manager(request: Request, asset: Asset) -> None:
    require_organization_write_access(request.user, asset.organization_id)
    if not can_manage_asset(request.user, asset):
        raise PermissionDenied("Only the asset owner or an organization admin may change this asset.")


def _delete(request: Request, asset: Asset, *, force: bool) -> str | None:
    """Soft-delete ``asset``; return a refusal reason when it is still referenced."""
    _require_manager(request, asset)
    references = asset_references([asset.id]).get(str(asset.id), [])
    if references and not force:
        return f"The asset is still used by: {', '.join(references)}. Pass force=true to delete it anyway."
    soft_delete_asset(asset)
    return None


class AssetListView(APIView):
    """``GET`` lists visible files (``?page=`` for pagination); ``POST`` uploads one file."""

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    throttle_classes = [BurstThrottle]

    @extend_schema(
        parameters=[
            OpenApiParameter("q", str, required=False),
            OpenApiParameter("page", int, required=False),
        ],
        responses={200: AssetSerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        organization = organization_for_request(request, required=True)
        queryset = (
            assets_visible_to(request.user, organization.id)
            .exclude(status=Asset.Status.DELETED)
            .order_by("-created_at")
        )
        if query := request.query_params.get("q"):
            queryset = queryset.filter(name__icontains=query) | queryset.filter(
                original_filename__icontains=query
            )
        if "page" in request.query_params:
            paginator = PageNumberPagination()
            page = paginator.paginate_queryset(queryset, request, view=self) or []
            return paginator.get_paginated_response(_serialize(list(page)))
        return Response(_serialize(list(queryset[:_LIST_LIMIT])))

    @extend_schema(request={"multipart/form-data": AssetUploadSerializer}, responses={201: AssetSerializer})
    def post(self, request: Request) -> Response:
        """Proxy a file to private Supabase Storage after byte-signature checks."""
        serializer = AssetUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization = organization_for_request(request, required=True)
        upload = serializer.validated_data["file"]
        if upload.size > settings.ASSET_MAX_UPLOAD_BYTES:
            return Response(
                {"detail": "File exceeds the configured upload limit."},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        try:
            content_type = validate_upload_type(upload.content_type or "")
        except SupabaseStorageError as exc:
            raise ValidationError({"file": str(exc)}) from exc
        content = upload.read()
        if not content or not content_matches_type(content[:512], content_type):
            raise ValidationError({"file": "The file's contents do not match its declared type."})
        if not supabase_storage_is_configured():
            return _unavailable()
        try:
            asset = register_uploaded_asset(
                content,
                owner=request.user,
                organization=organization,
                file_name=sanitize_file_name(upload.name or "upload"),
                content_type=content_type,
            )
        except (SupabaseStorageError, OSError) as exc:
            return Response({"detail": f"Upload failed: {exc}"}, status=status.HTTP_502_BAD_GATEWAY)
        visibility = serializer.validated_data["visibility"]
        if visibility != asset.visibility:
            asset.visibility = visibility
            asset.save(update_fields=["visibility", "updated_at"])
        return Response(_serialize([asset])[0], status=status.HTTP_201_CREATED)


class SupabaseStorageUploadView(APIView):
    """Issue a short-lived, one-object Supabase Storage upload capability."""

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = SignatureRequestSerializer

    def post(self, request: Request) -> Response:
        serializer = SignatureRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data["bytes"] > settings.ASSET_MAX_UPLOAD_BYTES:
            return Response(
                {"detail": "File exceeds the configured upload limit."},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        if not supabase_storage_is_configured():
            return _unavailable()
        organization = organization_for_request(request, required=True)
        intent_id = uuid.uuid4()
        expires_at = timezone.now() + timezone.timedelta(seconds=settings.ASSET_UPLOAD_AUTH_TTL_SECONDS)
        folder = user_upload_folder(request.user, organization.id)
        file_name = f"{intent_id}-{sanitize_file_name(serializer.validated_data['originalFilename'])}"
        storage_key = f"{folder}/{file_name}"
        try:
            upload_url, provider_token = create_signed_upload(storage_key)
        except SupabaseStorageError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        intent = UploadIntent.objects.create(
            id=intent_id,
            owner=request.user,
            organization=organization,
            token=secrets.token_urlsafe(32),
            folder=folder,
            file_name=file_name,
            original_filename=serializer.validated_data["originalFilename"],
            content_type=serializer.validated_data["contentType"],
            expected_bytes=serializer.validated_data["bytes"],
            expires_at=expires_at,
        )
        return Response(
            {
                "uploadIntentId": str(intent.id),
                "uploadToken": intent.token,
                "uploadUrl": upload_url,
                "uploadMethod": "PUT",
                "uploadHeaders": {
                    "x-signature": provider_token,
                    "x-upsert": "false",
                    "Content-Type": serializer.validated_data["contentType"],
                    "cache-control": "private, max-age=31536000, immutable",
                },
                "expire": int(expires_at.timestamp()),
                "bucket": settings.SUPABASE_STORAGE_BUCKET,
                "storageKey": storage_key,
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
            asset = Asset.objects.filter(storage_object_id=intent.storage_object_key).first()
            if asset and asset.owner_id == request.user.id:
                return Response(_serialize([asset])[0], status=status.HTTP_200_OK)
            return Response({"detail": "Upload intent has already been consumed."}, status=409)
        if intent.status != UploadIntent.Status.PENDING or intent.expires_at <= timezone.now():
            UploadIntent.objects.filter(id=intent.id).update(status=UploadIntent.Status.EXPIRED)
            return Response({"detail": "Upload intent has expired."}, status=status.HTTP_410_GONE)
        storage_key = serializer.validated_data["storageKey"]
        if storage_key != f"{intent.folder.rstrip('/')}/{intent.file_name}":
            return Response(
                {"detail": "Uploaded asset is outside the authorized folder."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not supabase_storage_is_configured():
            return _unavailable()
        try:
            checksum, delivered_type, head = content_checksum(
                storage_key, expected_size=intent.expected_bytes
            )
        except Exception:
            return Response(
                {"detail": "Supabase Storage asset could not be verified."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not content_matches_type(head, intent.content_type):
            return Response(
                {"detail": "The uploaded bytes do not match the declared content type."},
                status=status.HTTP_409_CONFLICT,
            )
        if delivered_type and delivered_type != intent.content_type:
            return Response(
                {"detail": "The object content type does not match the upload intent."},
                status=status.HTTP_409_CONFLICT,
            )
        with transaction.atomic():
            locked_intent = UploadIntent.objects.select_for_update().get(id=intent.id)
            if locked_intent.status != UploadIntent.Status.PENDING:
                asset = Asset.objects.filter(storage_object_id=locked_intent.storage_object_key).first()
                if asset:
                    return Response(_serialize([asset])[0], status=status.HTTP_200_OK)
                return Response({"detail": "Upload intent is no longer available."}, status=409)
            if Asset.objects.filter(storage_object_id=storage_key).exists():
                return Response(
                    {"detail": "Supabase Storage object is already registered."},
                    status=status.HTTP_409_CONFLICT,
                )
            asset = Asset.objects.create(
                owner=request.user,
                organization=organization,
                storage_object_id=storage_key,
                storage_key=storage_key,
                storage_bucket=settings.SUPABASE_STORAGE_BUCKET,
                storage_url="",
                resource_type="file",
                format=intent.original_filename.rsplit(".", 1)[-1][:50]
                if "." in intent.original_filename
                else "",
                bytes=intent.expected_bytes,
                version=0,
                original_filename=intent.original_filename,
                name=intent.original_filename,
                metadata={
                    "content_type": delivered_type or intent.content_type,
                },
                checksum_sha256=checksum,
                provider_fingerprint=provider_identity_fingerprint(
                    {
                        "bucket": settings.SUPABASE_STORAGE_BUCKET,
                        "key": storage_key,
                        "size": intent.expected_bytes,
                    }
                ),
                provenance={
                    "provider": "supabase-storage",
                    "origin": "direct-upload",
                    "bucket": settings.SUPABASE_STORAGE_BUCKET,
                    "storage_key": storage_key,
                    "upload_intent_id": str(intent.id),
                    "verified_at": timezone.now().isoformat(),
                    "verified_by": str(request.user.id),
                    "fingerprintVersion": FINGERPRINT_VERSION,
                },
                last_verified_at=timezone.now(),
            )
            locked_intent.status = UploadIntent.Status.COMPLETED
            locked_intent.storage_object_key = storage_key
            locked_intent.completed_at = timezone.now()
            locked_intent.save(update_fields=["status", "storage_object_key", "completed_at"])
            add_outbox_event(
                "asset.created",
                str(asset.id),
                {
                    "assetId": str(asset.id),
                    "storageKey": storage_key,
                    "ownerId": str(request.user.id),
                    "organizationId": str(organization.id),
                    "resourceType": asset.resource_type,
                    "bytes": asset.bytes,
                    "origin": "direct-upload",
                },
            )
        return Response(_serialize([asset])[0], status=status.HTTP_201_CREATED)


class AssetAccessView(APIView):
    """Return a short-lived provider URL only after asset authorization."""

    permission_classes = [IsAuthenticated]
    serializer_class = AssetAccessResponseSerializer

    def post(self, request: Request, id) -> Response:
        asset = assets_visible_to(request.user).filter(id=id, status=Asset.Status.READY).first()
        if asset is None:
            return Response({"detail": "Asset not found."}, status=status.HTTP_404_NOT_FOUND)
        if not supabase_storage_is_configured():
            return _unavailable()
        return Response(
            {
                "assetId": str(asset.id),
                "url": generate_signed_delivery_url(asset.storage_key),
                "expiresIn": settings.ASSET_SIGNED_URL_TTL_SECONDS,
            }
        )


class AssetDownloadView(APIView):
    """Stream an asset's bytes through the API (no provider URL leaves the server)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={(200, "application/octet-stream"): bytes})
    def get(self, request: Request, id) -> StreamingHttpResponse | Response:
        asset = assets_visible_to(request.user).filter(id=id, status=Asset.Status.READY).first()
        if asset is None:
            return Response({"detail": "Asset not found."}, status=status.HTTP_404_NOT_FOUND)
        if not supabase_storage_is_configured():
            return _unavailable()
        response = StreamingHttpResponse(stream_file(asset.storage_key), content_type=asset.content_type)
        response["Content-Length"] = str(asset.bytes)
        response["Content-Disposition"] = f'attachment; filename="{sanitize_file_name(asset.display_name)}"'
        response["X-Content-Type-Options"] = "nosniff"
        return response


class AssetDetailView(APIView):
    """Read, rename/re-share, or soft-delete an asset; provider deletion is delayed."""

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]

    @extend_schema(responses={200: AssetSerializer})
    def get(self, request: Request, id) -> Response:
        asset = _visible_asset(request, id, include_deleted=True)
        return Response(_serialize([asset])[0])

    @extend_schema(request=AssetUpdateSerializer, responses={200: AssetSerializer})
    def patch(self, request: Request, id) -> Response:
        asset = _visible_asset(request, id)
        _require_manager(request, asset)
        serializer = AssetUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        fields = []
        if "name" in serializer.validated_data:
            asset.name = serializer.validated_data["name"].strip()[:500]
            fields.append("name")
        if "visibility" in serializer.validated_data:
            asset.visibility = serializer.validated_data["visibility"]
            fields.append("visibility")
        if fields:
            asset.save(update_fields=[*fields, "updated_at"])
        return Response(_serialize([asset])[0])

    @extend_schema(parameters=[OpenApiParameter("force", bool, required=False)], responses={204: None})
    def delete(self, request: Request, id) -> Response:
        asset = _visible_asset(request, id)
        refusal = _delete(request, asset, force=request.query_params.get("force") in {"1", "true"})
        if refusal:
            return Response({"detail": refusal}, status=status.HTTP_409_CONFLICT)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AssetRestoreView(APIView):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]

    @extend_schema(request=None, responses={200: AssetSerializer})
    def post(self, request: Request, id) -> Response:
        asset = _visible_asset(request, id, include_deleted=True)
        _require_manager(request, asset)
        if not restore_asset(asset):
            return Response(
                {"detail": "Only soft-deleted assets whose provider file still exists can be restored."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(_serialize([asset])[0])


class AssetBulkDeleteView(APIView):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]

    @extend_schema(
        request=BulkDeleteSerializer,
        responses={
            200: inline_serializer(
                "AssetBulkDeleteResult",
                {
                    "deleted": serializers.ListField(child=serializers.UUIDField()),
                    "skipped": serializers.ListField(child=serializers.DictField()),
                },
            )
        },
    )
    def post(self, request: Request) -> Response:
        serializer = BulkDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        deleted, skipped = [], []
        assets = {
            str(asset.id): asset
            for asset in assets_visible_to(request.user)
            .filter(id__in=serializer.validated_data["ids"])
            .exclude(status=Asset.Status.DELETED)
        }
        for asset_id in map(str, serializer.validated_data["ids"]):
            asset = assets.get(asset_id)
            if asset is None:
                skipped.append({"id": asset_id, "reason": "not found"})
                continue
            try:
                refusal = _delete(request, asset, force=serializer.validated_data["force"])
            except PermissionDenied as exc:
                refusal = str(exc.detail)
            if refusal:
                skipped.append({"id": asset_id, "reason": refusal})
            else:
                deleted.append(asset_id)
        return Response({"deleted": deleted, "skipped": skipped})


class AssetAttachView(APIView):
    """Attach a readable file to a conversation the caller can write to."""

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]

    @extend_schema(request=AttachSerializer, responses={200: AssetSerializer})
    def post(self, request: Request, id) -> Response:
        from apps.conversations.models import Conversation

        asset = _visible_asset(request, id)
        serializer = AttachSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conversation = tenant_scoped_queryset(
            Conversation.objects.filter(id=serializer.validated_data["conversationId"]),
            request.user,
            organization_id=asset.organization_id,
        ).first()
        if conversation is None:
            raise NotFound("Conversation not found in the asset's organization.")
        require_organization_write_access(request.user, asset.organization_id)
        ConversationAttachment.objects.get_or_create(
            asset=asset, conversation=conversation, defaults={"attached_by": request.user}
        )
        return Response(_serialize([asset])[0])
