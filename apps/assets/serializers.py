from django.conf import settings
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.assets.models import Asset


class SignatureRequestSerializer(serializers.Serializer):
    originalFilename = serializers.CharField(max_length=500)
    contentType = serializers.CharField(max_length=255)
    bytes = serializers.IntegerField(min_value=1)

    def validate_contentType(self, value):
        normalized = value.split(";", 1)[0].strip().lower()
        if normalized not in settings.ASSET_ALLOWED_CONTENT_TYPES:
            raise serializers.ValidationError("This content type is not allowed for asset uploads.")
        return normalized


class CompleteUploadSerializer(serializers.Serializer):
    uploadIntentId = serializers.UUIDField()
    uploadToken = serializers.CharField(max_length=100)
    fileId = serializers.CharField(max_length=500)
    filePath = serializers.CharField(max_length=1000)


class AssetAccessResponseSerializer(serializers.Serializer):
    assetId = serializers.UUIDField()
    url = serializers.URLField()
    expiresIn = serializers.IntegerField(min_value=1)


class AssetSerializer(serializers.ModelSerializer):
    """Frontend ``FileItem`` contract plus provider/provenance detail."""

    name = serializers.CharField(source="display_name", read_only=True)
    mimeType = serializers.CharField(source="content_type", read_only=True)
    size = serializers.IntegerField(source="bytes", read_only=True)
    blobKey = serializers.CharField(source="imagekit_file_id", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)
    usedIn = serializers.SerializerMethodField()
    ownerId = serializers.UUIDField(source="owner_id", read_only=True, allow_null=True)
    originalFilename = serializers.CharField(source="original_filename", read_only=True)
    imagekitFileId = serializers.CharField(source="imagekit_file_id", read_only=True)
    imagekitFilePath = serializers.CharField(source="imagekit_file_path", read_only=True)
    resourceType = serializers.CharField(source="resource_type", read_only=True)
    checksumSha256 = serializers.CharField(source="checksum_sha256", read_only=True)
    deletedAt = serializers.DateTimeField(source="deleted_at", read_only=True, allow_null=True)

    class Meta:
        model = Asset
        fields = (
            "id",
            "name",
            "mimeType",
            "size",
            "blobKey",
            "createdAt",
            "updatedAt",
            "usedIn",
            "visibility",
            "status",
            "ownerId",
            "originalFilename",
            "imagekitFileId",
            "imagekitFilePath",
            "resourceType",
            "format",
            "bytes",
            "checksumSha256",
            "deletedAt",
        )
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_usedIn(self, obj: Asset) -> list[str]:
        references = self.context.get("references")
        if references is None:
            from apps.assets.services import asset_references

            references = asset_references([obj.id])
        return list(references.get(str(obj.id), []))


class AssetUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=500, required=False, allow_blank=False)
    visibility = serializers.ChoiceField(choices=Asset.Visibility.choices, required=False)


class AssetUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    visibility = serializers.ChoiceField(
        choices=Asset.Visibility.choices, required=False, default=Asset.Visibility.PRIVATE
    )


class BulkDeleteSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=100)
    force = serializers.BooleanField(required=False, default=False)


class AttachSerializer(serializers.Serializer):
    conversationId = serializers.UUIDField()
