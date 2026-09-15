from __future__ import annotations

from rest_framework import serializers

from apps.conversions.models import ConversionJob

ALLOWED_MATRIX = {
    ('pdf', 'txt'): 'pypdf',
    ('pdf', 'md'): 'pypdf',
    ('md', 'pdf'): 'weasyprint',
    ('html', 'pdf'): 'weasyprint',
    ('md', 'docx'): 'python-docx',
    ('txt', 'pdf'): 'weasyprint',
    ('png', 'jpg'): 'pillow',
    ('jpg', 'png'): 'pillow',
    ('png', 'webp'): 'pillow',
    ('jpg', 'webp'): 'pillow',
    ('webp', 'png'): 'pillow',
    ('webp', 'jpg'): 'pillow',
    ('docx', 'pdf'): 'libreoffice',
}

MAX_CONVERSION_INPUT_BYTES = 50 * 1024 * 1024
MAX_CONVERSION_PAGES = 500


class ConversionCreateSerializer(serializers.Serializer):
    input_format = serializers.CharField(max_length=20)
    output_format = serializers.CharField(max_length=20)
    content = serializers.CharField(allow_blank=True, required=False, default='')
    file = serializers.FileField(required=False)


class ConversionJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConversionJob
        fields = (
            'id', 'input_filename', 'input_format', 'output_format', 'status',
            'input_bytes', 'output_bytes', 'reserved_credits', 'output_url',
            'error_message', 'created_at', 'updated_at',
        )
        read_only_fields = fields
