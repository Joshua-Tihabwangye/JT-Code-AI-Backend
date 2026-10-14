from __future__ import annotations

from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.visualize import catalog, samples
from apps.visualize.models import (
    Dashboard,
    DashboardMember,
    DashboardVersion,
    PersonalView,
    SavedChart,
    Theme,
)

_MAX_JSON = 20_000


def _bounded_object(value: Any, label: str) -> dict[str, Any]:
    import json

    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise serializers.ValidationError(f"{label} must be an object.")
    if len(json.dumps(value, default=str)) > _MAX_JSON:
        raise serializers.ValidationError(f"{label} is too large.")
    return value


class StyleSerializer(serializers.Serializer):
    palette = serializers.ChoiceField(choices=list(catalog.PALETTES), required=False)
    theme = serializers.ChoiceField(choices=list(catalog.THEMES), required=False)
    title = serializers.CharField(max_length=200, required=False, allow_blank=True)
    xTitle = serializers.CharField(max_length=120, required=False, allow_blank=True)
    yTitle = serializers.CharField(max_length=120, required=False, allow_blank=True)
    showLegend = serializers.BooleanField(required=False)
    colors = serializers.ListField(
        child=serializers.RegexField(r"^#[0-9A-Fa-f]{6}$"), required=False, max_length=12
    )
    maxSeries = serializers.IntegerField(required=False, min_value=2, max_value=30)


class RenderRequestSerializer(serializers.Serializer):
    datasetId = serializers.UUIDField()
    chartType = serializers.ChoiceField(choices=[chart.id for chart in catalog.CHARTS])
    fields = serializers.JSONField(required=False)
    options = serializers.JSONField(required=False)
    style = StyleSerializer(required=False)

    def validate_fields(self, value: Any) -> dict[str, Any]:
        return _bounded_object(value, "fields")

    def validate_options(self, value: Any) -> dict[str, Any]:
        return _bounded_object(value, "options")


class WorkbookChartSerializer(RenderRequestSerializer):
    title = serializers.CharField(max_length=200, required=False, allow_blank=True)


class WorkbookRequestSerializer(serializers.Serializer):
    """An Excel workbook of the charts on a Studio page: their values, optionally with Excel charts."""

    title = serializers.CharField(max_length=200, required=False, allow_blank=True)
    graphs = serializers.BooleanField(default=True)
    charts = WorkbookChartSerializer(many=True)

    def validate_charts(self, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not 1 <= len(value) <= 12:
            raise serializers.ValidationError("Send between 1 and 12 charts.")
        if len({str(item["datasetId"]) for item in value}) > 8:
            raise serializers.ValidationError("Charts may use at most 8 datasets.")
        return value


class PredictRequestSerializer(serializers.Serializer):
    datasetId = serializers.UUIDField()
    task = serializers.ChoiceField(choices=[task.id for task in catalog.PREDICTION_TASKS])
    model = serializers.CharField(max_length=40, required=False, allow_blank=True)
    fields = serializers.JSONField(required=False)
    options = serializers.JSONField(required=False)
    style = StyleSerializer(required=False)

    def validate_fields(self, value: Any) -> dict[str, Any]:
        return _bounded_object(value, "fields")

    def validate_options(self, value: Any) -> dict[str, Any]:
        return _bounded_object(value, "options")

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        task = catalog.PREDICTION_TASKS_BY_ID[attrs["task"]]
        if attrs.get("model") and attrs["model"] not in dict(task.models):
            raise serializers.ValidationError({"model": "Unknown model for this task."})
        return attrs


class AssistantRequestSerializer(serializers.Serializer):
    prompt = serializers.CharField(max_length=2000, trim_whitespace=True)
    datasetId = serializers.UUIDField(required=False, allow_null=True)
    context = serializers.JSONField(required=False)

    def validate_context(self, value: Any) -> dict[str, Any]:
        return _bounded_object(value, "context")


class SampleRequestSerializer(serializers.Serializer):
    sampleId = serializers.ChoiceField(choices=[sample.id for sample in samples.SAMPLES])
    name = serializers.CharField(max_length=200, required=False, allow_blank=True)


class SaveResultSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False, allow_blank=True)


class DatasetImportSerializer(serializers.Serializer):
    file = serializers.FileField()
    name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    format = serializers.ChoiceField(choices=[fmt["id"] for fmt in catalog.IMPORT_FORMATS], required=False)
    sheet = serializers.CharField(max_length=100, required=False, allow_blank=True)


class CellEditSerializer(serializers.Serializer):
    row = serializers.IntegerField(min_value=0)
    column = serializers.CharField(max_length=200)
    value = serializers.CharField(max_length=10_000, allow_blank=True, allow_null=True, trim_whitespace=False)


class DatasetEditSerializer(serializers.Serializer):
    """The user's own corrections; the scan never edits data by itself."""

    edits = CellEditSerializer(many=True, required=False, max_length=5000)
    deleteRows = serializers.ListField(
        child=serializers.IntegerField(min_value=0), required=False, max_length=50_000
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs.get("edits") and not attrs.get("deleteRows"):
            raise serializers.ValidationError("Nothing to save.")
        return attrs


class OrganizeRequestSerializer(serializers.Serializer):
    steps = serializers.JSONField()
    saveAs = serializers.CharField(max_length=200, required=False, allow_blank=True)

    def validate_steps(self, value: Any) -> dict[str, Any]:
        value = _bounded_object(value, "steps")
        unknown = set(value) - {"clean", "transform"}
        if unknown:
            raise serializers.ValidationError(f"Unknown steps: {', '.join(sorted(unknown))}.")
        return value


class SavedChartSerializer(serializers.ModelSerializer):
    datasetId = serializers.UUIDField(source="dataset_id", read_only=True, allow_null=True)
    datasetName = serializers.SerializerMethodField()
    chartType = serializers.CharField(source="chart_type", read_only=True)
    isShared = serializers.BooleanField(source="is_shared", read_only=True)
    ownerId = serializers.IntegerField(source="owner_id", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)
    option = serializers.SerializerMethodField()

    class Meta:
        model = SavedChart
        fields = [
            "id",
            "title",
            "description",
            "chartType",
            "datasetId",
            "datasetName",
            "config",
            "option",
            "explanation",
            "stats",
            "notes",
            "isShared",
            "ownerId",
            "createdAt",
            "updatedAt",
        ]
        read_only_fields = fields

    @extend_schema_field(OpenApiTypes.STR)
    def get_datasetName(self, obj: SavedChart) -> str:
        return obj.dataset.name if obj.dataset_id and obj.dataset else ""

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_option(self, obj: SavedChart) -> dict[str, Any] | None:
        return obj.option if self.context.get("include_option") else None


class SavedChartCreateSerializer(serializers.Serializer):
    datasetId = serializers.UUIDField(required=False)
    chartType = serializers.ChoiceField(choices=[chart.id for chart in catalog.CHARTS], required=False)
    fields = serializers.JSONField(required=False)
    options = serializers.JSONField(required=False)
    style = StyleSerializer(required=False)
    resultId = serializers.RegexField(r"^[0-9a-f]{32}$", required=False)
    chartIndex = serializers.IntegerField(min_value=0, max_value=20, required=False)
    title = serializers.CharField(max_length=200, required=False, allow_blank=True)
    description = serializers.CharField(max_length=5000, required=False, allow_blank=True)
    isShared = serializers.BooleanField(required=False)

    def validate_fields(self, value: Any) -> dict[str, Any]:
        return _bounded_object(value, "fields")

    def validate_options(self, value: Any) -> dict[str, Any]:
        return _bounded_object(value, "options")

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if attrs.get("resultId"):
            return attrs
        if not attrs.get("datasetId") or not attrs.get("chartType"):
            raise serializers.ValidationError("Provide datasetId and chartType, or a prediction resultId.")
        return attrs


class SavedChartUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(max_length=5000, required=False, allow_blank=True)
    isShared = serializers.BooleanField(required=False)
    chartType = serializers.ChoiceField(choices=[chart.id for chart in catalog.CHARTS], required=False)
    fields = serializers.JSONField(required=False)
    options = serializers.JSONField(required=False)
    style = StyleSerializer(required=False)


# --- Dashboards ----------------------------------------------------------------------------------


class DashboardSerializer(serializers.ModelSerializer):
    """A dashboard; ``spec`` is validated by ``dashboard_spec.clean_spec`` in the view."""

    spec = serializers.JSONField(required=False)
    isShared = serializers.BooleanField(source="is_shared", required=False)
    ownerId = serializers.UUIDField(source="owner_id", read_only=True)
    ownerName = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()
    version = serializers.IntegerField(required=False)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Dashboard
        fields = [
            "id",
            "name",
            "description",
            "spec",
            "thumbnail",
            "isShared",
            "version",
            "ownerId",
            "ownerName",
            "role",
            "createdAt",
            "updatedAt",
        ]
        read_only_fields = ["id", "ownerId", "ownerName", "role", "createdAt", "updatedAt"]
        extra_kwargs = {"name": {"max_length": 200}, "thumbnail": {"required": False, "allow_blank": True}}

    def validate_thumbnail(self, value: str) -> str:
        if value and (not value.startswith("data:image/") or len(value) > 300_000):
            raise serializers.ValidationError("The thumbnail must be an image data URL under 300 KB.")
        return value

    @extend_schema_field(OpenApiTypes.STR)
    def get_ownerName(self, obj: Dashboard) -> str:
        owner = obj.owner
        return (owner.get_full_name() or owner.email or owner.username) if owner else ""

    @extend_schema_field(serializers.ChoiceField(choices=["owner", "editor", "viewer"]))
    def get_role(self, obj: Dashboard) -> str:
        roles = self.context.get("roles") or {}
        return roles.get(str(obj.id), "viewer")


class DashboardListSerializer(DashboardSerializer):
    """Dashboards home cards: everything but the full spec."""

    pages = serializers.SerializerMethodField()
    datasetIds = serializers.SerializerMethodField()

    class Meta(DashboardSerializer.Meta):
        fields = [f for f in DashboardSerializer.Meta.fields if f != "spec"] + ["pages", "datasetIds"]

    @extend_schema_field(OpenApiTypes.INT)
    def get_pages(self, obj: Dashboard) -> int:
        return len((obj.spec or {}).get("pages") or [])

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_datasetIds(self, obj: Dashboard) -> list[str]:
        return sorted(
            {
                (v.get("data") or {}).get("datasetId")
                for p in (obj.spec or {}).get("pages") or []
                for v in p.get("visuals") or []
                if (v.get("data") or {}).get("datasetId")
            }
        )


class DashboardVersionSerializer(serializers.ModelSerializer):
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    createdBy = serializers.UUIDField(source="created_by_id", read_only=True, allow_null=True)

    class Meta:
        model = DashboardVersion
        fields = ["number", "name", "createdAt", "createdBy"]


class DashboardMemberSerializer(serializers.ModelSerializer):
    userId = serializers.UUIDField(source="user_id")
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = DashboardMember
        fields = ["userId", "email", "role"]


class PersonalViewSerializer(serializers.ModelSerializer):
    isDefault = serializers.BooleanField(source="is_default", required=False)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = PersonalView
        fields = ["id", "name", "state", "isDefault", "updatedAt"]
        read_only_fields = ["id", "updatedAt"]


class ThemeSerializer(serializers.ModelSerializer):
    isShared = serializers.BooleanField(source="is_shared", required=False)
    ownerId = serializers.UUIDField(source="owner_id", read_only=True)

    class Meta:
        model = Theme
        fields = ["id", "name", "spec", "isShared", "ownerId"]
        read_only_fields = ["id", "ownerId"]


class VisualsRequestSerializer(serializers.Serializer):
    """A batch of visuals to draw with the current filters (one page, or a few tiles)."""

    visuals = serializers.ListField(child=serializers.JSONField(), min_length=1, max_length=60)
    filters = serializers.ListField(child=serializers.JSONField(), required=False, max_length=80)
    theme = serializers.JSONField(required=False)
    mode = serializers.ChoiceField(choices=["light", "dark"], default="light")
    today = serializers.DateField(required=False)
    dashboardId = serializers.UUIDField(required=False)


class AutoDashboardSerializer(serializers.Serializer):
    datasetId = serializers.UUIDField()
    name = serializers.CharField(max_length=200, required=False, allow_blank=True)
