from rest_framework import serializers

from apps.documents.models import Document


class DocumentSerializer(serializers.ModelSerializer):
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = Document
        fields = (
            'id', 'title', 'template', 'template_version', 'content', 'status',
            'version', 'provenance', 'download_url', 'page_count',
            'error_message', 'createdAt', 'updatedAt',
        )
        read_only_fields = (
            'id', 'template_version', 'status', 'version', 'provenance',
            'download_url', 'page_count', 'error_message', 'createdAt', 'updatedAt',
        )


class DocumentCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Document
        fields = ('title', 'template', 'content')
        extra_kwargs = {'template': {'required': False}}


class DocumentRenderSerializer(serializers.Serializer):
    format = serializers.ChoiceField(choices=('pdf', 'docx'), default='pdf')
    version = serializers.IntegerField(min_value=1, required=False)