"""Dashboard AI endpoints (create a page, ask, auto-format, suggest) and "explain this change".

The model calls are metered as chat messages and fall back to free rules; whatever they return is
validated with :mod:`apps.visualize.dashboard_spec` before the client sees it, and the client only
applies it after the user accepts the preview.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.models import Dataset
from apps.analytics.services import AnalysisError
from apps.core.throttling import BurstThrottle, ChatThrottle
from apps.visualize import dashboard_ai, dashboard_spec, themes, visual_plan
from apps.visualize.dashboard_views import (
    DashboardThrottle,
    SpecInvalid,
    VisualizeQueryThrottle,
    _organization,
    datasets_for,
)
from apps.visualize.serializers import (
    AIAskSerializer,
    AIFormatSerializer,
    AIPageSerializer,
    AISuggestSerializer,
    ExplainSerializer,
)


def _dataset(request: Request, data: dict[str, Any]) -> Dataset:
    dataset_id = str(data["datasetId"])
    found = datasets_for(request, {dataset_id}, data.get("dashboardId"))
    if dataset_id not in found:
        raise NotFound("Dataset not found.")
    return found[dataset_id]


def _filters(raw: Any) -> list[dict[str, Any]]:
    try:
        return [dashboard_spec.clean_filter(f, "Filters") for f in raw or []]
    except dashboard_spec.SpecError as exc:
        raise SpecInvalid(str(exc)) from exc


class _AIView(APIView):
    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        return [ChatThrottle(), BurstThrottle()]


class AIPageView(_AIView):
    """A new page from a prompt, as a preview the user applies or discards."""

    @extend_schema(request=AIPageSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        serializer = AIPageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dataset = _dataset(request, serializer.validated_data)
        result = dashboard_ai.create_page(
            dataset=dataset,
            prompt=serializer.validated_data["prompt"],
            organization=_organization(request),
            user=request.user,
        )
        try:
            spec = dashboard_spec.clean_spec(
                {
                    "pages": [result["page"]],
                    "calculations": result.get("calculations") or [],
                    "parameters": result.get("parameters") or [],
                }
            )
        except dashboard_spec.SpecError as exc:
            raise SpecInvalid(f"The proposed page is not valid: {exc}") from exc
        return Response(
            {
                "page": spec["pages"][0],
                "calculations": spec["calculations"],
                "parameters": spec["parameters"],
                "source": result["source"],
                "usage": result["usage"],
            }
        )


class AIAskView(_AIView):
    """Answer a question with one visual (pinnable) and a sentence computed from its data."""

    @extend_schema(request=AIAskSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        serializer = AIAskSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        dataset = _dataset(request, data)
        try:
            answer = dashboard_ai.ask(
                dataset=dataset,
                question=data["question"],
                filters=_filters(data.get("filters")),
                organization=_organization(request),
                user=request.user,
                theme=data.get("theme") or themes.DEFAULT_THEME,
                mode=data["mode"],
                today=data.get("today") or timezone.localdate(),
            )
            answer["visual"] = dashboard_spec.clean_visual(answer["visual"], "Answer")
            answer["filters"] = _filters(answer["filters"])
        except (dashboard_ai.AIError, dashboard_spec.SpecError) as exc:
            raise SpecInvalid(str(exc)) from exc
        return Response(answer)


class AIFormatView(_AIView):
    """Restyle a dashboard from a request such as "executive dark, compact and aligned"."""

    @extend_schema(request=AIFormatSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        serializer = AIFormatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            spec = dashboard_spec.clean_spec(serializer.validated_data["spec"])
            result = dashboard_ai.auto_format(
                prompt=serializer.validated_data["prompt"],
                spec=spec,
                organization=_organization(request),
                user=request.user,
            )
            result["spec"] = dashboard_spec.clean_spec(result["spec"])
        except dashboard_spec.SpecError as exc:
            raise SpecInvalid(str(exc)) from exc
        return Response(result)


class AISuggestView(APIView):
    """Visuals that would suit the page (rule-based, free)."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [DashboardThrottle]

    @extend_schema(request=AISuggestSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        serializer = AISuggestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dataset = _dataset(request, serializer.validated_data)
        items = dashboard_ai.suggest(dataset=dataset, existing=serializer.validated_data.get("visuals") or [])
        try:
            for item in items:
                item["visual"] = dashboard_spec.clean_visual(item["visual"], item["name"])
        except dashboard_spec.SpecError as exc:
            raise SpecInvalid(str(exc)) from exc
        return Response({"suggestions": items})


class ExplainView(APIView):
    """Why a measure changed in a period, by the members that contributed most (no AI, free)."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [VisualizeQueryThrottle]

    @extend_schema(request=ExplainSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        serializer = ExplainSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        dataset = _dataset(request, data)
        measure = data["measure"] if isinstance(data["measure"], dict) else {}
        try:
            result = dashboard_ai.explain_change(
                dataset=dataset,
                measure={k: measure[k] for k in ("field", "agg", "label") if k in measure} or {"field": "*"},
                date_field=data["dateField"],
                period=data["period"],
                filters=_filters(data.get("filters")),
                today=data.get("today") or timezone.localdate(),
                grain=data.get("grain"),
            )
        except (dashboard_ai.AIError, visual_plan.PlanError, AnalysisError) as exc:
            raise SpecInvalid(str(exc)) from exc
        return Response(result)
