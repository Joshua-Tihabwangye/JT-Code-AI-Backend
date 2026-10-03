from rest_framework import serializers

from apps.assets.models import Asset


class SignatureRequestSerializer(serializers.Serializer):
    originalFilename = serializers.CharField(max_length=500)
    contentType = serializers.CharField(max_length=255)
    bytes = serializers.IntegerField(min_value=1)

    def validate_contentType(self, value):
        normalized = value.split(";", 1)[0].strip().lower()
        allowed = {
            "application/json",
            "application/pdf",
            "application/zip",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "text/csv",
            "text/markdown",
            "text/plain",
        }
        if not (normalized.startswith("image/") or normalized in allowed):
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
    originalFilename = serializers.CharField(source="original_filename", read_only=True)
    imagekitFileId = serializers.CharField(source="imagekit_file_id", read_only=True)
    imagekitFilePath = serializers.CharField(source="imagekit_file_path", read_only=True)
    resourceType = serializers.CharField(source="resource_type", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    checksumSha256 = serializers.CharField(source="checksum_sha256", read_only=True)

    class Meta:
        model = Asset
        fields = (
            "id",
            "originalFilename",
            "imagekitFileId",
            "imagekitFilePath",
            "resourceType",
            "format",
            "bytes",
            "status",
            "checksumSha256",
            "createdAt",
        )
