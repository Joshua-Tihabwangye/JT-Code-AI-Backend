from drf_spectacular.utils import OpenApiTypes, extend_schema_field
from rest_framework import serializers

from apps.analytics.models import AnalysisRun, Dataset, DatasetGrant, Visualization
from apps.analytics.services import AnalysisError, validate_transform_spec
from apps.assets.imagekit import generate_signed_delivery_url
from apps.assets.models import Asset


class DatasetSerializer(serializers.ModelSerializer):
    source_type = serializers.SerializerMethodField()

    class Meta:
        model = Dataset
        fields = [
            "id",
            "owner",
            "name",
            "mime_type",
            "source_type",
            "schema",
            "row_count",
            "byte_size",
            "source_checksum_sha256",
            "is_shared",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    @extend_schema_field(OpenApiTypes.STR)
    def get_source_type(self, obj) -> str:
        return "asset" if obj.asset_id else "inline"


class DatasetCreateSerializer(serializers.ModelSerializer):
    inline_data = serializers.CharField(write_only=True, required=False, allow_blank=False)
    asset = serializers.PrimaryKeyRelatedField(queryset=Asset.objects.all(), write_only=True, required=False)

    class Meta:
        model = Dataset
        fields = ["name", "mime_type", "inline_data", "asset", "is_shared"]

    def validate_mime_type(self, value: str) -> str:
        from django.conf import settings

        normalized = value.split(";", 1)[0].strip().lower()
        if normalized not in settings.ANALYTICS_ALLOWED_MIME_TYPES:
            raise serializers.ValidationError("Only CSV datasets are supported.")
        return normalized

    def validate(self, attrs):
        if bool(attrs.get("inline_data")) == bool(attrs.get("asset")):
            raise serializers.ValidationError("Supply exactly one of inline_data or asset.")
        return attrs


class DatasetUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Dataset
        fields = ["name", "is_shared"]


class DatasetGrantSerializer(serializers.ModelSerializer):
    class Meta:
        model = DatasetGrant
        fields = ["id", "dataset", "user", "permission", "created_at"]
        read_only_fields = ["id", "created_at"]


class AnalysisRunSerializer(serializers.ModelSerializer):
    result_download_url = serializers.SerializerMethodField()

    class Meta:
        model = AnalysisRun
        fields = [
            "id",
            "dataset",
            "status",
            "transform",
            "profile",
            "result_schema",
            "result_preview",
            "result_download_url",
            "error_message",
            "started_at",
            "completed_at",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "profile",
            "result_schema",
            "result_preview",
            "result_download_url",
            "error_message",
            "started_at",
            "completed_at",
            "created_at",
        ]

    def validate_transform(self, value):
        try:
            return validate_transform_spec(value)
        except AnalysisError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    @extend_schema_field(OpenApiTypes.URI)
    def get_result_download_url(self, obj) -> str | None:
        if obj.status != AnalysisRun.Status.COMPLETED or not obj.result_asset_id:
            return None
        if obj.result_asset.status != Asset.Status.READY:
            return None
        return generate_signed_delivery_url(obj.result_asset.imagekit_file_path)


class VisualizationSerializer(serializers.ModelSerializer):
    artifact_url = serializers.SerializerMethodField()

    class Meta:
        model = Visualization
        fields = [
            "id",
            "analysis_run",
            "kind",
            "x_column",
            "y_column",
            "status",
            "plotly_spec",
            "artifact_url",
            "artifact_checksum_sha256",
            "result_schema",
            "error_message",
            "started_at",
            "completed_at",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "plotly_spec",
            "artifact_url",
            "artifact_checksum_sha256",
            "result_schema",
            "error_message",
            "started_at",
            "completed_at",
            "created_at",
        ]

    def validate(self, attrs):
        kind = attrs.get("kind")
        if kind != Visualization.Kind.HISTOGRAM and not attrs.get("y_column"):
            raise serializers.ValidationError({"y_column": "This field is required for this chart type."})
        return attrs

    @extend_schema_field(OpenApiTypes.URI)
    def get_artifact_url(self, obj) -> str | None:
        if obj.status != Visualization.Status.READY or not obj.artifact_asset_id:
            return None
        if obj.artifact_asset.status != Asset.Status.READY:
            return None
        return generate_signed_delivery_url(obj.artifact_asset.imagekit_file_path)
