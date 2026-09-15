from rest_framework import serializers
from apps.assets.models import Asset

class SignatureRequestSerializer(serializers.Serializer):
    originalFilename = serializers.CharField(max_length=500)
    contentType = serializers.CharField(max_length=255)
    bytes = serializers.IntegerField(min_value=1)

class CompleteUploadSerializer(serializers.Serializer):
    publicId = serializers.CharField(max_length=500)
    secureUrl = serializers.URLField(max_length=1000)
    resourceType = serializers.ChoiceField(choices=('image', 'raw', 'video'))
    format = serializers.CharField(max_length=50, required=False, allow_blank=True)
    bytes = serializers.IntegerField(min_value=0)
    version = serializers.IntegerField(min_value=0)
    originalFilename = serializers.CharField(max_length=500)

class AssetSerializer(serializers.ModelSerializer):
    originalFilename = serializers.CharField(source='original_filename', read_only=True)
    secureUrl = serializers.URLField(source='secure_url', read_only=True)
    resourceType = serializers.CharField(source='resource_type', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    class Meta:
        model = Asset
        fields = ('id', 'originalFilename', 'secureUrl', 'resourceType', 'format', 'bytes', 'status', 'createdAt')
