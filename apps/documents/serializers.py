from drf_spectacular.utils import extend_schema_serializer
from rest_framework import serializers

from apps.documents.models import Document, DocumentVersion


class DocumentVersionSerializer(serializers.ModelSerializer):
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = DocumentVersion
        fields = ("id", "version", "content", "createdAt")
        read_only_fields = fields


@extend_schema_serializer(component_name="RenderedDocument")
class DocumentSerializer(serializers.ModelSerializer):
    """The frontend ``AppDocument`` shape plus rendering state."""

    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)
    versions = DocumentVersionSerializer(many=True, read_only=True)

    class Meta:
        model = Document
        fields = (
            "id",
            "title",
            "template",
            "template_version",
            "content",
            "status",
            "version",
            "favorite",
            "versions",
            "provenance",
            "download_url",
            "rendered_asset",
            "page_count",
            "error_message",
            "createdAt",
            "updatedAt",
        )
        read_only_fields = (
            "id",
            "template_version",
            "status",
            "version",
            "provenance",
            "download_url",
            "rendered_asset",
            "page_count",
            "error_message",
            "createdAt",
            "updatedAt",
        )


class DocumentCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = ("title", "template", "content")
        extra_kwargs = {"template": {"required": False}}


class DocumentRenderSerializer(serializers.Serializer):
    format = serializers.ChoiceField(choices=("pdf", "docx"), default="pdf")
    version = serializers.IntegerField(min_value=1, required=False)
