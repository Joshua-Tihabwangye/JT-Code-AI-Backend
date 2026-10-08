from django.conf import settings
from drf_spectacular.utils import OpenApiTypes, extend_schema_field
from rest_framework import serializers

from apps.analytics.models import AnalysisRun, Dataset, DatasetGrant, Visualization
from apps.analytics.services import AnalysisError, validate_transform_spec
from apps.assets.models import Asset
from apps.assets.supabase_storage import generate_signed_delivery_url


def _signed(asset) -> str | None:
    if asset is None or asset.status != Asset.Status.READY:
        return None
    return generate_signed_delivery_url(asset.storage_key)


class AnalysisResultSchemaSerializer(serializers.Serializer):
    """Versioned analysis result schema (``AnalysisRun.result_schema``)."""

    version = serializers.ChoiceField(choices=["1"])
    format = serializers.ChoiceField(choices=["csv"])
    columns = serializers.ListField(child=serializers.CharField(), max_length=1000)
    dtypes = serializers.DictField(child=serializers.CharField())
    rows = serializers.IntegerField(min_value=0)
    bytes = serializers.IntegerField(min_value=1)
    checksumSha256 = serializers.RegexField(r"^[0-9a-f]{64}$")


class VisualizationResultSchemaSerializer(serializers.Serializer):
    """Versioned visualization result schema (``Visualization.result_schema``)."""

    version = serializers.ChoiceField(choices=["1"])
    kind = serializers.ChoiceField(choices=Visualization.Kind.choices)
    staticFormat = serializers.ChoiceField(choices=["png"])
    interactiveFormat = serializers.ChoiceField(choices=["plotly-json"])
    bytes = serializers.IntegerField(min_value=1)
    specBytes = serializers.IntegerField(min_value=1)
    points = serializers.IntegerField(min_value=1)
    checksumSha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    specChecksumSha256 = serializers.RegexField(r"^[0-9a-f]{64}$")


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
            "profile",
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
    asset = serializers.UUIDField(write_only=True, required=False)

    class Meta:
        model = Dataset
        fields = ["name", "mime_type", "inline_data", "asset", "is_shared"]

    def validate_mime_type(self, value: str) -> str:
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
    result_schema = serializers.SerializerMethodField()

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
        if obj.status != AnalysisRun.Status.COMPLETED:
            return None
        return _signed(obj.result_asset)

    @extend_schema_field(AnalysisResultSchemaSerializer(allow_null=True))
    def get_result_schema(self, obj) -> dict | None:
        return obj.result_schema or None


class VisualizationSerializer(serializers.ModelSerializer):
    artifact_url = serializers.SerializerMethodField()
    spec_url = serializers.SerializerMethodField()
    result_schema = serializers.SerializerMethodField()

    class Meta:
        model = Visualization
        fields = [
            "id",
            "analysis_run",
            "kind",
            "x_column",
            "y_column",
            "color_column",
            "title",
            "status",
            "plotly_spec",
            "artifact_url",
            "spec_url",
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
            "spec_url",
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
        if kind == Visualization.Kind.PIE and attrs.get("color_column"):
            raise serializers.ValidationError({"color_column": "Pie charts do not support a color column."})
        run = attrs.get("analysis_run")
        columns = set((run.result_schema or {}).get("columns", [])) if run is not None else set()
        if columns:
            for field in ("x_column", "y_column", "color_column"):
                if attrs.get(field) and attrs[field] not in columns:
                    raise serializers.ValidationError({field: "Unknown column in the analysis result."})
        return attrs

    @extend_schema_field(OpenApiTypes.URI)
    def get_artifact_url(self, obj) -> str | None:
        return _signed(obj.artifact_asset) if obj.status == Visualization.Status.READY else None

    @extend_schema_field(OpenApiTypes.URI)
    def get_spec_url(self, obj) -> str | None:
        return _signed(obj.spec_asset) if obj.status == Visualization.Status.READY else None

    @extend_schema_field(VisualizationResultSchemaSerializer(allow_null=True))
    def get_result_schema(self, obj) -> dict | None:
        return obj.result_schema or None
