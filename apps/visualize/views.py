"""Data visualization API: catalog, data import/organize/export, rendering, prediction,
the AI assistant, saved charts and reports.

Rendering, insights and recommendations are free (throttled); predictions are
metered as analysis runs; the assistant is metered per prompt and is the only
endpoint that calls an AI model.
"""

from __future__ import annotations

import base64
import re
import uuid
from typing import Any

from django.conf import settings
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.access import datasets_analyzable_by, datasets_visible_to
from apps.analytics.models import Dataset
from apps.analytics.sandbox import SandboxError
from apps.analytics.serializers import DatasetSerializer
from apps.analytics.services import AnalysisError, dataset_bytes
from apps.core.throttling import AnalyticsThrottle, BurstThrottle, ChatThrottle, PerUserRateThrottle
from apps.identity.authorization import (
    organization_for_request,
    require_organization_write_access,
    user_has_role,
)
from apps.identity.models import Role
from apps.usage.models import Feature
from apps.usage.services import metered
from apps.visualize import assistant, catalog, recommend, samples, services
from apps.visualize.models import SavedChart
from apps.visualize.serializers import (
    AssistantRequestSerializer,
    DatasetEditSerializer,
    DatasetImportSerializer,
    OrganizeRequestSerializer,
    PredictRequestSerializer,
    RenderRequestSerializer,
    SampleRequestSerializer,
    SavedChartCreateSerializer,
    SavedChartSerializer,
    SavedChartUpdateSerializer,
    SaveResultSerializer,
    WorkbookRequestSerializer,
)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
IMPORT_EXTENSIONS = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".tab": "tsv",
    ".txt": "txt",
    ".json": "json",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".xlsx": "xlsx",
}


class VisualizationError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "The visualization request could not be completed."
    default_code = "visualization_error"


class EngineUnavailable(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "The visualization engine failed. Please try again."
    default_code = "visualization_engine_failed"


class VisualizeThrottle(PerUserRateThrottle):
    scope = "visualize"


def _engine(
    operation: str, dataset: Dataset | None = None, content: bytes = b"", **request: Any
) -> dict[str, Any]:
    try:
        return services.engine(operation, dataset, content, **request)
    except AnalysisError as exc:
        raise VisualizationError(str(exc)) from exc
    except SandboxError as exc:
        raise EngineUnavailable() from exc


def _organization(request: Request) -> Any:
    return organization_for_request(request, required=True)


def _is_admin(user: Any, organization: Any) -> bool:
    return user_has_role(user, Role.RoleType.ADMIN, organization.id)


def _dataset(request: Request, dataset_id: Any, *, analyze: bool = True) -> Dataset:
    organization = _organization(request)
    queryset = (datasets_analyzable_by if analyze else datasets_visible_to)(request.user, organization)
    dataset = queryset.select_related("asset").filter(id=dataset_id).first()
    if dataset is None:
        raise NotFound("Dataset not found.")
    return dataset


def _flag(request: Request, name: str, default: bool) -> bool:
    value = request.query_params.get(name)
    return default if value is None else value.lower() in {"1", "true", "yes"}


def _download(content: bytes, *, mime: str, filename: str) -> HttpResponse:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-")[:120] or "download"
    response = HttpResponse(content, content_type=mime)
    response["Content-Disposition"] = f'attachment; filename="{safe}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "no-store"
    return response


# --- Catalog -------------------------------------------------------------------------------------


class CatalogView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request: Request) -> Response:
        payload = catalog.public_catalog()
        payload["samples"] = [
            {
                "id": sample.id,
                "name": sample.name,
                "description": sample.description,
                "audience": sample.audience,
            }
            for sample in samples.SAMPLES
        ]
        payload["limits"] = {
            "maxUploadBytes": settings.ANALYTICS_MAX_DATASET_BYTES,
            "maxRows": settings.ANALYTICS_MAX_DATASET_ROWS,
            "maxColumns": settings.ANALYTICS_MAX_DATASET_COLUMNS,
            "maxChartPoints": settings.ANALYTICS_MAX_CHART_POINTS,
        }
        payload["pricing"] = {
            "render": 0,
            "insights": 0,
            "predict": "analysis_runs",
            "assistant": "chat_messages",
        }
        return Response(payload)


# --- Datasets: import, samples, preview, insights, organize, export ----------------------------


def _created(dataset: Dataset) -> Response:
    return Response(DatasetSerializer(dataset).data, status=status.HTTP_201_CREATED)


class SampleDatasetView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [VisualizeThrottle]

    @extend_schema(request=SampleRequestSerializer, responses={201: DatasetSerializer})
    def post(self, request: Request) -> Response:
        organization = _organization(request)
        require_organization_write_access(request.user, organization.id)
        serializer = SampleRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sample = samples.SAMPLES_BY_ID[serializer.validated_data["sampleId"]]
        content = samples.sample_csv(sample.id).encode("utf-8")
        profile = _engine("viz_preview", content=content, mime_type="text/csv", rows=1)["profile"]
        dataset = services.create_dataset(
            name=serializer.validated_data.get("name") or sample.name,
            csv=content,
            profile=profile,
            owner=request.user,
            organization=organization,
            provenance={"kind": "visualize-sample", "sample": sample.id},
        )
        return _created(dataset)


class UploadTooLarge(APIException):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_detail = "The file exceeds the upload limit."
    default_code = "upload_too_large"


def _import_upload(request: Request) -> tuple[bytes, dict[str, Any], str, str, str]:
    """Parse an uploaded CSV/TSV/TXT/JSON/JSONL/XLSX file in the sandbox into normalized CSV."""
    serializer = DatasetImportSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    upload = serializer.validated_data["file"]
    if upload.size > settings.ANALYTICS_MAX_DATASET_BYTES:
        raise UploadTooLarge()
    name = upload.name or "dataset"
    extension = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    fmt = serializer.validated_data.get("format") or IMPORT_EXTENSIONS.get(extension)
    if fmt is None:
        raise VisualizationError(
            "Unsupported file type. Use CSV, TSV, TXT, JSON, JSON Lines or Excel (.xlsx)."
        )
    content = upload.read(settings.ANALYTICS_MAX_DATASET_BYTES + 1)
    output = _engine(
        "viz_import", content=content, format=fmt, sheet=serializer.validated_data.get("sheet") or None
    )
    dataset_name = serializer.validated_data.get("name") or name.rsplit(".", 1)[0][:200] or "Imported data"
    return base64.b64decode(output["csv"]), output["profile"], fmt, name, dataset_name


class DatasetImportView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [VisualizeThrottle, BurstThrottle]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(request=DatasetImportSerializer, responses={201: DatasetSerializer})
    def post(self, request: Request) -> Response:
        organization = _organization(request)
        require_organization_write_access(request.user, organization.id)
        csv, profile, fmt, name, dataset_name = _import_upload(request)
        try:
            dataset = services.create_dataset(
                name=dataset_name,
                csv=csv,
                profile=profile,
                owner=request.user,
                organization=organization,
                provenance={"kind": "visualize-import", "format": fmt, "file": name[:200]},
            )
        except AnalysisError as exc:
            raise VisualizationError(str(exc)) from exc
        return _created(dataset)


class DatasetPreviewView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [VisualizeThrottle]

    @extend_schema(
        parameters=[
            OpenApiParameter("offset", int, required=False),
            OpenApiParameter("rows", int, required=False),
            OpenApiParameter(
                "issues", bool, required=False, description="Include data-quality flags per cell."
            ),
            OpenApiParameter(
                "filter", str, required=False, description="Only rows with issues: 'issues' or an issue type."
            ),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request: Request, dataset_id: uuid.UUID) -> Response:
        from apps.visualize.viz_quality import ISSUE_TYPES

        dataset = _dataset(request, dataset_id, analyze=False)
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
            rows = max(1, min(int(request.query_params.get("rows", 100)), 500))
        except ValueError as exc:
            raise VisualizationError("offset and rows must be integers.") from exc
        issue_filter = request.query_params.get("filter", "")
        if issue_filter and issue_filter != "issues" and issue_filter not in ISSUE_TYPES:
            raise VisualizationError("Unknown issue filter.")
        output = _engine(
            "viz_preview",
            dataset,
            offset=offset,
            rows=rows,
            issues=request.query_params.get("issues") in {"1", "true", "yes"},
            issue_filter=issue_filter,
        )
        if not dataset.profile.get("columnProfiles"):
            services._store_profile(dataset, output["profile"])
        return Response({"datasetId": str(dataset.id), "name": dataset.name, **output})


class DatasetInsightsView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [VisualizeThrottle]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request: Request, dataset_id: uuid.UUID) -> Response:
        dataset = _dataset(request, dataset_id)
        output = _engine("viz_insights", dataset)
        if not dataset.profile.get("columnProfiles"):
            services._store_profile(dataset, output["profile"])
        return Response(
            {
                "datasetId": str(dataset.id),
                "insights": output["insights"],
                "recommendations": recommend.recommendations(output["profile"]),
            }
        )


class DatasetRecommendView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter("chart", str, required=False),
            OpenApiParameter("task", str, required=False),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    def get(self, request: Request, dataset_id: uuid.UUID) -> Response:
        dataset = _dataset(request, dataset_id, analyze=False)
        profile = services.ensure_profile(dataset)
        chart_id = request.query_params.get("chart")
        payload: dict[str, Any] = {
            "recommendations": recommend.recommendations(profile),
            "roles": recommend.column_roles(profile),
        }
        if chart_id:
            if chart_id not in catalog.CHARTS_BY_ID:
                raise VisualizationError("Unknown chart type.")
            payload["fields"] = recommend.auto_fields(chart_id, profile)
        return Response(payload)


class DatasetOrganizeView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [VisualizeThrottle]

    @extend_schema(
        request=OrganizeRequestSerializer, responses={200: OpenApiTypes.OBJECT, 201: DatasetSerializer}
    )
    def post(self, request: Request, dataset_id: uuid.UUID) -> Response:
        dataset = _dataset(request, dataset_id)
        serializer = OrganizeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        save_as = serializer.validated_data.get("saveAs")
        if save_as:
            require_organization_write_access(request.user, dataset.organization_id)
        steps = serializer.validated_data["steps"]
        output = _engine("viz_organize", dataset, steps=steps, others=_other_datasets(request, steps))
        if not save_as:
            return Response({"profile": output["profile"], "preview": output["preview"]})
        try:
            created = services.create_dataset(
                name=save_as,
                csv=base64.b64decode(output["csv"]),
                profile=output["profile"],
                owner=request.user,
                organization=dataset.organization,
                provenance={"kind": "visualize-organize", "source_dataset_id": str(dataset.id)},
            )
        except AnalysisError as exc:
            raise VisualizationError(str(exc)) from exc
        return _created(created)


def _save_visual(request: Request, organization: Any, data: dict[str, Any]) -> SavedChart:
    """A dashboard visual saved to My visuals, with its drawn chart for previews and exports."""
    from django.utils import timezone

    from apps.visualize import dashboards, themes

    visual = data["visual"]
    dataset = _dataset(request, visual["data"]["datasetId"])
    output = dashboards.render(
        {str(dataset.id): dataset}, [visual], [], themes.DEFAULT_THEME, "light", timezone.localdate()
    )["visuals"][0]
    if output.get("status") == "error":
        raise VisualizationError(output.get("message") or "This visual could not be drawn.")
    return SavedChart.objects.create(
        organization=organization,
        owner=request.user,
        dataset=dataset,
        title=data.get("title") or output.get("title") or visual["type"].title(),
        chart_type=f"visual:{visual['type']}",
        config={"visual": visual},
        option=output.get("option") or {},
        explanation={"insight": output.get("insight") or ""},
        description=data.get("description", ""),
        is_shared=data.get("isShared", False),
    )


def _other_datasets(request: Request, steps: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Datasets a pipeline merges or appends, as engine inputs (only ones the user may analyse)."""
    from apps.visualize import viz_steps

    others: dict[str, dict[str, str]] = {}
    for other_id in viz_steps.other_datasets(steps.get("pipeline") or []):
        try:
            uuid.UUID(other_id)
        except ValueError as exc:
            raise VisualizationError("Merge and append need a dataset id.") from exc
        other = _dataset(request, other_id)
        others[other_id] = {
            "data": base64.b64encode(dataset_bytes(other)).decode("ascii"),
            "mime_type": other.mime_type,
        }
    return others


class DatasetQualityView(APIView):
    """Scan a dataset for duplicates, missing values and IDs, wrong types and anomalies (no AI)."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [VisualizeThrottle]

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    def get(self, request: Request, dataset_id: uuid.UUID) -> Response:
        dataset = _dataset(request, dataset_id, analyze=False)
        return Response({"datasetId": str(dataset.id), **_engine("viz_quality", dataset)})


def _manageable(request: Request, dataset_id: uuid.UUID) -> Dataset:
    from apps.analytics.access import can_manage_dataset

    dataset = _dataset(request, dataset_id, analyze=False)
    if not can_manage_dataset(request.user, dataset):
        raise PermissionDenied("Only the dataset owner or an organization admin may change its data.")
    return dataset


class DatasetEditView(APIView):
    """Save the user's own corrections: cell edits and row deletions."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [VisualizeThrottle]

    @extend_schema(request=DatasetEditSerializer, responses={200: DatasetSerializer})
    def post(self, request: Request, dataset_id: uuid.UUID) -> Response:
        dataset = _manageable(request, dataset_id)
        serializer = DatasetEditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        output = _engine(
            "viz_edit",
            dataset,
            edits=serializer.validated_data.get("edits", []),
            delete_rows=serializer.validated_data.get("deleteRows", []),
        )
        try:
            services.update_dataset_content(
                dataset, csv=base64.b64decode(output["csv"]), profile=output["profile"], user=request.user
            )
        except AnalysisError as exc:
            raise VisualizationError(str(exc)) from exc
        return Response(DatasetSerializer(dataset).data)


class DatasetReplaceView(APIView):
    """Replace a dataset's data with a corrected file (e.g. fixed in Excel and re-uploaded)."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [VisualizeThrottle, BurstThrottle]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(request=DatasetImportSerializer, responses={200: DatasetSerializer})
    def post(self, request: Request, dataset_id: uuid.UUID) -> Response:
        dataset = _manageable(request, dataset_id)
        csv, profile = _import_upload(request)[:2]
        try:
            services.update_dataset_content(dataset, csv=csv, profile=profile, user=request.user)
        except AnalysisError as exc:
            raise VisualizationError(str(exc)) from exc
        return Response(DatasetSerializer(dataset).data)


class WorldMapView(APIView):
    """The world GeoJSON (Natural Earth, public domain) that map charts register with ECharts."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses={(200, "application/json"): OpenApiTypes.OBJECT})
    def get(self, request: Request) -> HttpResponse:
        response = HttpResponse(services._static("world.json"), content_type="application/json")
        response["Cache-Control"] = "private, max-age=86400"
        return response


class DatasetExportView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [VisualizeThrottle]

    @extend_schema(
        parameters=[
            OpenApiParameter("type", str, enum=["csv", "xlsx", "json"], required=False),
            OpenApiParameter("graphs", bool, required=False, description="Excel only: add suggested charts."),
        ],
        responses={(200, "application/octet-stream"): OpenApiTypes.BINARY},
    )
    def get(self, request: Request, dataset_id: uuid.UUID) -> HttpResponse:
        dataset = _dataset(request, dataset_id, analyze=False)
        fmt = request.query_params.get("type", "csv")
        if fmt not in {"csv", "xlsx", "json"}:
            raise VisualizationError("format must be csv, xlsx or json.")
        extra: dict[str, Any] = {}
        if fmt == "xlsx" and _flag(request, "graphs", False):
            ideas = recommend.recommendations(services.ensure_profile(dataset))[:6]
            extra = {
                "graphs": True,
                "charts": [
                    {
                        "dataset": 0,
                        "chart_type": idea["chartType"],
                        "fields": idea["fields"],
                        "options": idea.get("options") or {},
                        "subtitle": idea["name"],
                    }
                    for idea in ideas
                ],
            }
        output = _engine("viz_export", dataset, format=fmt, name=dataset.name, **extra)
        return _download(services.decode(output), mime=output["mimeType"], filename=f"{dataset.name}.{fmt}")


# --- Rendering, prediction and the assistant ----------------------------------------------------


def _render(dataset: Dataset, data: dict[str, Any]) -> dict[str, Any]:
    return _engine(
        "viz_render",
        dataset,
        chart_type=data["chartType"],
        fields=data.get("fields") or {},
        options=data.get("options") or {},
        style=data.get("style") or {},
    )


class RenderView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [VisualizeThrottle]

    @extend_schema(request=RenderRequestSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        serializer = RenderRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dataset = _dataset(request, serializer.validated_data["datasetId"])
        return Response(_render(dataset, serializer.validated_data))


class PredictView(APIView):
    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        return [AnalyticsThrottle(), BurstThrottle()]

    @extend_schema(request=PredictRequestSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        serializer = PredictRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        dataset = _dataset(request, data["datasetId"])
        with metered(
            organization=dataset.organization,
            user=request.user,
            feature=Feature.ANALYSIS_RUNS,
            source_type="viz_prediction",
            source_id=uuid.uuid4(),
        ):
            output = _engine(
                "viz_predict",
                dataset,
                task=data["task"],
                fields=data.get("fields") or {},
                model=data.get("model") or "",
                options=data.get("options") or {},
                style=data.get("style") or {},
            )
        result_csv = output.pop("result")
        result_id = services.remember_result(
            dataset.organization_id,
            request.user.id,
            {
                "csv": result_csv,
                "datasetId": str(dataset.id),
                "task": output["task"],
                "charts": output["charts"],
                "name": dataset.name,
                "modelName": output.get("modelName", ""),
                "metrics": output.get("metrics") or [],
                "summary": output.get("summary") or [],
            },
        )
        return Response({**output, "resultId": result_id, "datasetId": str(dataset.id)})


class PredictionResultView(APIView):
    permission_classes = [IsAuthenticated]

    def _result(self, request: Request, result_id: str) -> dict[str, Any]:
        organization = _organization(request)
        result = services.recall_result(organization.id, request.user.id, result_id)
        if result is None:
            raise NotFound("The prediction result has expired; run the prediction again.")
        return result

    @extend_schema(
        parameters=[
            OpenApiParameter("type", str, enum=["csv", "xlsx"], required=False),
            OpenApiParameter(
                "graphs", bool, required=False, description="Excel only: draw each chart as an Excel chart."
            ),
        ],
        responses={(200, "application/octet-stream"): OpenApiTypes.BINARY},
    )
    def get(self, request: Request, result_id: str) -> HttpResponse:
        result = self._result(request, result_id)
        filename = f"{result['name']}-{result['task']}"
        fmt = request.query_params.get("type", "csv")
        if fmt == "csv":
            return _download(base64.b64decode(result["csv"]), mime="text/csv", filename=f"{filename}.csv")
        if fmt != "xlsx":
            raise VisualizationError("format must be csv or xlsx.")
        task = catalog.PREDICTION_TASKS_BY_ID.get(result["task"])
        summary: list[tuple[str, str]] = [
            ("Prediction", task.name if task else result["task"]),
            ("Dataset", result["name"]),
            ("Model", result.get("modelName") or ""),
        ]
        for metric in result.get("metrics") or []:
            summary.append((str(metric.get("label")), _metric_text(metric)))
        for index, line in enumerate(result.get("summary") or [], start=1):
            summary.append((f"Finding {index}", str(line)))
        output = _engine(
            "viz_workbook",
            summary=summary,
            tables=[{"name": "Predictions", "data": result["csv"]}],
            charts=[
                {
                    "title": chart.get("title"),
                    "note": (chart.get("explanation") or {}).get("insight", ""),
                    "option": chart.get("option") or {},
                }
                for chart in result.get("charts") or []
            ],
            graphs=_flag(request, "graphs", True),
        )
        return _download(services.decode(output), mime=XLSX, filename=f"{filename}.xlsx")

    @extend_schema(request=SaveResultSerializer, responses={201: DatasetSerializer})
    def post(self, request: Request, result_id: str) -> Response:
        """Save a prediction result as a new dataset (no new credits)."""
        organization = _organization(request)
        require_organization_write_access(request.user, organization.id)
        result = self._result(request, result_id)
        csv = base64.b64decode(result["csv"])
        profile = _engine("viz_preview", content=csv, mime_type="text/csv", rows=1)["profile"]
        serializer = SaveResultSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        name = serializer.validated_data.get("name") or f"{result['name']} – {result['task']} results"
        try:
            dataset = services.create_dataset(
                name=name,
                csv=csv,
                profile=profile,
                owner=request.user,
                organization=organization,
                provenance={
                    "kind": "visualize-prediction",
                    "source_dataset_id": result["datasetId"],
                    "task": result["task"],
                },
            )
        except AnalysisError as exc:
            raise VisualizationError(str(exc)) from exc
        return _created(dataset)


def _metric_text(metric: dict[str, Any]) -> str:
    value = metric.get("value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if metric.get("format") == "percent":
            return f"{value:.1%}"
        return f"{value:,.4g}" if abs(value) < 1e6 else f"{value:,.0f}"
    return "" if value is None else str(value)


class WorkbookView(APIView):
    """Excel workbook of a Studio page: each chart's values on its own sheet, with an Excel chart."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [VisualizeThrottle]

    @extend_schema(
        request=WorkbookRequestSerializer, responses={(200, "application/octet-stream"): OpenApiTypes.BINARY}
    )
    def post(self, request: Request) -> HttpResponse:
        serializer = WorkbookRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        order: dict[str, int] = {}
        datasets: list[dict[str, Any]] = []
        charts: list[dict[str, Any]] = []
        for item in data["charts"]:
            key = str(item["datasetId"])
            if key not in order:
                dataset = _dataset(request, item["datasetId"])
                try:
                    content = dataset_bytes(dataset)
                except AnalysisError as exc:
                    raise VisualizationError(str(exc)) from exc
                order[key] = len(datasets)
                datasets.append({"name": dataset.name, "data": base64.b64encode(content).decode("ascii")})
            known = catalog.CHARTS_BY_ID[item["chartType"]]
            charts.append(
                {
                    "dataset": order[key],
                    "chart_type": item["chartType"],
                    "fields": item.get("fields") or {},
                    "options": item.get("options") or {},
                    "style": item.get("style") or {},
                    "title": item.get("title") or "",
                    "subtitle": f"{known.name} · {datasets[order[key]]['name']}",
                }
            )
        title = data.get("title") or "Charts"
        output = _engine(
            "viz_workbook",
            datasets=datasets,
            summary=[("Workbook", title), ("Charts", str(len(charts)))],
            charts=charts,
            graphs=data["graphs"],
        )
        return _download(services.decode(output), mime=XLSX, filename=f"{title}.xlsx")


class AssistantView(APIView):
    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        return [ChatThrottle(), BurstThrottle()]

    @extend_schema(request=AssistantRequestSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        serializer = AssistantRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        organization = _organization(request)
        profile = None
        if data.get("datasetId"):
            profile = services.ensure_profile(_dataset(request, data["datasetId"], analyze=False))
        answer = assistant.ask(
            prompt=data["prompt"],
            profile=profile,
            context=data.get("context"),
            organization=organization,
            user=request.user,
        )
        return Response(answer)


# --- Saved charts and reports ------------------------------------------------------------------


def _visible(model: Any, request: Request) -> Any:
    organization = _organization(request)
    queryset = model.objects.filter(organization=organization)
    if _is_admin(request.user, organization):
        return queryset
    from django.db.models import Q

    return queryset.filter(Q(owner=request.user) | Q(is_shared=True))


def _require_manage(request: Request, instance: Any) -> None:
    if instance.owner_id != request.user.id and not _is_admin(request.user, instance.organization):
        raise PermissionDenied("Only the owner or an organization admin may change this.")


class SavedChartViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    lookup_field = "id"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return SavedChart.objects.none()
        queryset = _visible(SavedChart, self.request).select_related("dataset").order_by("-updated_at")
        if self.action == "list":
            return queryset.defer("option")
        return queryset

    def get_serializer_class(self):
        if self.action == "create":
            return SavedChartCreateSerializer
        if self.action == "partial_update":
            return SavedChartUpdateSerializer
        return SavedChartSerializer

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "include_option": self.action != "list"}

    def create(self, request: Request, *args, **kwargs) -> Response:
        organization = _organization(request)
        require_organization_write_access(request.user, organization.id)
        serializer = SavedChartCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data.get("resultId"):
            result = services.recall_result(organization.id, request.user.id, data["resultId"])
            if result is None:
                raise NotFound("The prediction result has expired; run the prediction again.")
            index = data.get("chartIndex", 0)
            if not 0 <= index < len(result["charts"]):
                raise VisualizationError("chartIndex is out of range.")
            source = result["charts"][index]
            dataset = _dataset(request, result["datasetId"])
            chart = SavedChart.objects.create(
                organization=organization,
                owner=request.user,
                dataset=dataset,
                title=data.get("title") or source["title"],
                chart_type=f"prediction:{source['id']}",
                config={"prediction": result["task"]},
                option=source["option"],
                explanation=source.get("explanation") or {},
                description=data.get("description", ""),
                is_shared=data.get("isShared", False),
            )
        elif data.get("visual"):
            chart = _save_visual(request, organization, data)
        else:
            dataset = _dataset(request, data["datasetId"])
            rendered = _render(dataset, data)
            chart = SavedChart.objects.create(
                organization=organization,
                owner=request.user,
                dataset=dataset,
                title=data.get("title") or rendered["title"],
                chart_type=rendered["chartType"],
                config={
                    "fields": rendered["fields"],
                    "options": rendered["options"],
                    "style": data.get("style") or {},
                },
                option=rendered["option"],
                explanation=rendered["explanation"],
                stats=rendered["stats"],
                notes=rendered["notes"],
                description=data.get("description", ""),
                is_shared=data.get("isShared", False),
            )
        return Response(
            SavedChartSerializer(chart, context={"include_option": True}).data, status=status.HTTP_201_CREATED
        )

    def partial_update(self, request: Request, *args, **kwargs) -> Response:
        chart = self.get_object()
        _require_manage(request, chart)
        serializer = SavedChartUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        for field, attribute in (
            ("title", "title"),
            ("description", "description"),
            ("isShared", "is_shared"),
        ):
            if field in data:
                setattr(chart, attribute, data[field])
        if any(
            key in data for key in ("fields", "options", "style", "chartType")
        ) and not chart.chart_type.startswith("prediction:"):
            if chart.dataset is None:
                raise VisualizationError("The chart's dataset was deleted; it can no longer be edited.")
            dataset = _dataset(request, chart.dataset_id)
            config = {
                "chartType": data.get("chartType", chart.chart_type),
                "fields": data.get("fields", chart.config.get("fields", {})),
                "options": data.get("options", chart.config.get("options", {})),
                "style": {
                    **chart.config.get("style", {}),
                    **data.get("style", {}),
                    "title": data.get("title", chart.title),
                },
            }
            rendered = _render(dataset, config)
            chart.chart_type = rendered["chartType"]
            chart.config = {
                "fields": rendered["fields"],
                "options": rendered["options"],
                "style": config["style"],
            }
            chart.option, chart.stats, chart.notes = rendered["option"], rendered["stats"], rendered["notes"]
            chart.explanation = rendered["explanation"]
        chart.save()
        return Response(SavedChartSerializer(chart, context={"include_option": True}).data)

    def perform_destroy(self, instance: SavedChart) -> None:
        _require_manage(self.request, instance)
        instance.delete()

    @extend_schema(request=None, responses={200: SavedChartSerializer})
    @action(detail=True, methods=["post"])
    def refresh(self, request: Request, id=None) -> Response:
        """Re-render from the current dataset (charts are snapshots until refreshed)."""
        chart = self.get_object()
        _require_manage(request, chart)
        if chart.chart_type.startswith("prediction:") or chart.dataset_id is None:
            raise VisualizationError("Only dataset charts can be refreshed.")
        rendered = _render(
            _dataset(request, chart.dataset_id), {"chartType": chart.chart_type, **chart.config}
        )
        chart.option, chart.stats, chart.notes = rendered["option"], rendered["stats"], rendered["notes"]
        chart.explanation = rendered["explanation"]
        chart.save(update_fields=["option", "explanation", "stats", "notes", "updated_at"])
        return Response(SavedChartSerializer(chart, context={"include_option": True}).data)

    @extend_schema(
        parameters=[OpenApiParameter("type", str, enum=["html", "json"], required=False)],
        responses={(200, "text/html"): OpenApiTypes.BINARY},
    )
    @action(detail=True, methods=["get"])
    def export(self, request: Request, id=None) -> HttpResponse:
        chart = self.get_object()
        fmt = request.query_params.get("type", "html")
        if fmt == "json":
            data = SavedChartSerializer(chart, context={"include_option": True}).data
            return _download(_json_bytes(data), mime="application/json", filename=f"{chart.title}.json")
        if fmt != "html":
            raise VisualizationError("format must be html or json.")
        theme = (chart.config.get("style") or {}).get("theme", "light")
        page = services.report_html(
            title=chart.title, description=chart.description, theme=theme, blocks=[_chart_block(chart)]
        )
        return _download(
            page.encode("utf-8"), mime="text/html; charset=utf-8", filename=f"{chart.title}.html"
        )


def _json_bytes(data: Any) -> bytes:
    import json

    return json.dumps(data, default=str, indent=2).encode("utf-8")


def _chart_block(chart: SavedChart, note: str = "") -> dict[str, Any]:
    chart_type = chart.chart_type.split(":", 1)[-1]
    known = catalog.CHARTS_BY_ID.get(chart_type)
    subtitle = " · ".join(
        filter(
            None,
            [
                known.name if known else chart_type.replace("_", " ").title(),
                chart.dataset.name if chart.dataset else "",
            ],
        )
    )
    return {
        "type": "chart",
        "title": chart.title,
        "subtitle": subtitle,
        "option": chart.option,
        "explanation": chart.explanation,
        "stats": chart.stats,
        "note": note or chart.description,
    }
