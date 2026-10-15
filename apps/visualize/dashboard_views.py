"""Dashboards API: CRUD with optimistic versioning, snapshots, sharing, personal views, themes,
the starter auto-dashboard and the batched visuals endpoint."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from django.db import transaction
from django.db.models import Max, Q
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics.access import datasets_analyzable_by, datasets_visible_to
from apps.analytics.models import Dataset
from apps.core.throttling import PerUserRateThrottle
from apps.identity.authorization import (
    organization_for_request,
    require_organization_write_access,
    user_has_role,
)
from apps.identity.models import Role, UserOrganization
from apps.visualize import autodash, dashboard_export, dashboard_spec, dashboards, services, themes
from apps.visualize.models import Dashboard, DashboardMember, DashboardVersion, Measure, PersonalView, Theme
from apps.visualize.serializers import (
    AutoDashboardSerializer,
    DashboardExportSerializer,
    DashboardListSerializer,
    DashboardMemberSerializer,
    DashboardSerializer,
    DashboardVersionSerializer,
    MeasureSerializer,
    PersonalViewSerializer,
    ThemeSerializer,
    VisualsRequestSerializer,
)
from apps.visualize.views import _download

SNAPSHOT_EVERY = dt.timedelta(minutes=10)
MAX_VERSIONS = 100


class VisualizeQueryThrottle(PerUserRateThrottle):
    """Slicers and cross-filtering redraw pages often, so visuals have their own, higher budget."""

    scope = "visualize_query"


class DashboardThrottle(PerUserRateThrottle):
    scope = "visualize"


class VersionConflict(Exception):
    """Another save happened since the client loaded the dashboard (answered as HTTP 409)."""

    message = "This dashboard was changed elsewhere. Reload it, or save again to overwrite."

    def __init__(self, current: int) -> None:
        super().__init__(self.message)
        self.current = current


class ExportInvalid(APIException):
    status_code = 400
    default_code = "export_invalid"


def datasets_for(request: Request, wanted: set[str], dashboard_id: Any) -> dict[str, Dataset]:
    """Datasets the user may analyze — or, inside a dashboard shared with them, the datasets that
    dashboard uses (its owner's access applies, as in Power BI report sharing)."""
    organization = _organization(request)
    found = {str(d.id): d for d in datasets_analyzable_by(request.user, organization).filter(id__in=wanted)}
    missing = wanted - set(found)
    if missing and dashboard_id:
        dashboard = visible_dashboards(request).filter(id=dashboard_id).first()
        if dashboard is not None:
            usable = missing & (
                dashboard_spec.dataset_ids(dashboard.spec)
                | {
                    end["datasetId"]
                    for link in dashboard.spec.get("relationships") or []
                    for end in (link["from"], link["to"])
                }
            )
            for item in datasets_analyzable_by(dashboard.owner, organization).filter(id__in=usable):
                found[str(item.id)] = item
    return found


class SpecInvalid(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "dashboard_invalid"


def _organization(request: Request) -> Any:
    return organization_for_request(request, required=True)


def _is_admin(user: Any, organization: Any) -> bool:
    return user_has_role(user, Role.RoleType.ADMIN, organization.id)


def visible_dashboards(request: Request) -> Any:
    organization = _organization(request)
    queryset = Dashboard.objects.filter(organization=organization).select_related("owner")
    if _is_admin(request.user, organization):
        return queryset
    user = request.user
    return queryset.filter(Q(owner=user) | Q(is_shared=True) | Q(members__user=user)).distinct()


def role_for(user: Any, dashboard: Dashboard) -> str:
    """owner (or org admin) > editor member > viewer."""
    if dashboard.owner_id == user.id or _is_admin(user, dashboard.organization):
        return "owner"
    member = next((m for m in dashboard.members.all() if m.user_id == user.id), None)
    return member.role if member else "viewer"


def _require_edit(request: Request, dashboard: Dashboard) -> None:
    if role_for(request.user, dashboard) not in {"owner", "editor"}:
        raise PermissionDenied("You can view this dashboard but not change it.")
    require_organization_write_access(request.user, dashboard.organization_id)


def _require_owner(request: Request, dashboard: Dashboard) -> None:
    if role_for(request.user, dashboard) != "owner":
        raise PermissionDenied("Only the owner or an organization admin may do this.")


def _clean(spec: Any) -> dict[str, Any]:
    try:
        return dashboard_spec.clean_spec(spec or {})
    except dashboard_spec.SpecError as exc:
        raise SpecInvalid(str(exc)) from exc


def _check_datasets(request: Request, spec: dict[str, Any]) -> None:
    ids = dashboard_spec.dataset_ids(spec)
    if not ids:
        return
    organization = _organization(request)
    visible = {
        str(pk)
        for pk in datasets_visible_to(request.user, organization)
        .filter(id__in=ids)
        .values_list("id", flat=True)
    }
    missing = ids - visible
    if missing:
        raise SpecInvalid("Some visuals use datasets you cannot open.")


def _snapshot(dashboard: Dashboard, user: Any, name: str = "") -> DashboardVersion:
    number = (dashboard.versions.aggregate(top=Max("number"))["top"] or 0) + 1
    version = DashboardVersion.objects.create(
        dashboard=dashboard, number=number, name=name[:200], spec=dashboard.spec, created_by=user
    )
    stale = dashboard.versions.order_by("-number").values_list("pk", flat=True)[MAX_VERSIONS:]
    DashboardVersion.objects.filter(pk__in=list(stale)).delete()
    return version


class DashboardViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    throttle_classes = [DashboardThrottle]
    lookup_field = "id"
    http_method_names = ["get", "post", "patch", "put", "delete", "head", "options"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Dashboard.objects.none()
        return visible_dashboards(self.request).prefetch_related("members").order_by("-updated_at")

    def get_serializer_class(self):
        return DashboardListSerializer if self.action == "list" else DashboardSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if getattr(self, "swagger_fake_view", False) or not self.request.user.is_authenticated:
            return context
        if self.action == "list":
            context["roles"] = {str(d.id): role_for(self.request.user, d) for d in self.get_queryset()}
        elif self.kwargs.get("id"):
            dashboard = self.get_queryset().filter(id=self.kwargs["id"]).first()
            if dashboard:
                context["roles"] = {str(dashboard.id): role_for(self.request.user, dashboard)}
        return context

    def perform_create(self, serializer):
        organization = _organization(self.request)
        require_organization_write_access(self.request.user, organization.id)
        spec = _clean(serializer.validated_data.pop("spec", None))
        _check_datasets(self.request, spec)
        dashboard = serializer.save(organization=organization, owner=self.request.user, spec=spec, version=1)
        _snapshot(dashboard, self.request.user, "Created")
        serializer.context["roles"] = {str(dashboard.id): "owner"}

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        kwargs["partial"] = True
        try:
            return super().update(request, *args, **kwargs)
        except VersionConflict as conflict:
            body = {
                "code": "version_conflict",
                "message": conflict.message,
                "details": {"currentVersion": conflict.current},
            }
            return Response(body, status=status.HTTP_409_CONFLICT)

    def perform_update(self, serializer):
        dashboard: Dashboard = serializer.instance
        _require_edit(self.request, dashboard)
        data = serializer.validated_data
        with transaction.atomic():
            locked = Dashboard.objects.select_for_update().get(pk=dashboard.pk)
            expected = data.pop("version", None)
            if (
                expected is not None
                and expected != locked.version
                and not self.request.query_params.get("force")
            ):
                raise VersionConflict(locked.version)
            if (
                "is_shared" in data
                and locked.owner_id != self.request.user.id
                and not _is_admin(self.request.user, locked.organization)
            ):
                data.pop("is_shared")
            if "spec" in data:
                data["spec"] = _clean(data["spec"])
                _check_datasets(self.request, data["spec"])
                last = locked.versions.order_by("-number").first()
                if last is None or timezone.now() - last.created_at > SNAPSHOT_EVERY:
                    _snapshot(locked, self.request.user, "Autosave")
            serializer.save(version=locked.version + 1)

    def perform_destroy(self, instance: Dashboard) -> None:
        _require_owner(self.request, instance)
        instance.delete()

    @extend_schema(request=None, responses={201: DashboardSerializer})
    @action(detail=True, methods=["post"])
    def duplicate(self, request: Request, id=None) -> Response:
        source = self.get_object()
        organization = _organization(request)
        require_organization_write_access(request.user, organization.id)
        copy = Dashboard.objects.create(
            organization=organization,
            owner=request.user,
            name=f"{source.name} (copy)"[:200],
            description=source.description,
            spec=source.spec,
            thumbnail=source.thumbnail,
        )
        _snapshot(copy, request.user, f"Copied from {source.name}")
        body = DashboardSerializer(copy, context={"roles": {str(copy.id): "owner"}, "request": request}).data
        return Response(body, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=None, responses={200: DashboardVersionSerializer(many=True), 201: DashboardVersionSerializer}
    )
    @action(detail=True, methods=["get", "post"])
    def versions(self, request: Request, id=None) -> Response:
        dashboard = self.get_object()
        if request.method == "POST":
            _require_edit(request, dashboard)
            name = str((request.data or {}).get("name") or "Saved version")
            return Response(
                DashboardVersionSerializer(_snapshot(dashboard, request.user, name)).data, status=201
            )
        return Response(DashboardVersionSerializer(dashboard.versions.all()[:MAX_VERSIONS], many=True).data)

    @extend_schema(
        request=None,
        parameters=[OpenApiParameter("number", int, OpenApiParameter.PATH, description="Version number")],
        responses={200: DashboardSerializer},
    )
    @action(detail=True, methods=["post"], url_path=r"versions/(?P<number>[^/.]+)/restore")
    def restore(self, request: Request, id=None, number: str = "0") -> Response:
        dashboard = self.get_object()
        _require_edit(request, dashboard)
        version = dashboard.versions.filter(number=int(number)).first() if number.isdigit() else None
        if version is None:
            raise NotFound("That version does not exist.")
        with transaction.atomic():
            locked = Dashboard.objects.select_for_update().get(pk=dashboard.pk)
            _snapshot(locked, request.user, f"Before restoring version {version.number}")
            locked.spec = _clean(version.spec)
            locked.version += 1
            locked.save(update_fields=["spec", "version", "updated_at"])
        context = {"roles": {str(locked.id): role_for(request.user, locked)}, "request": request}
        return Response(DashboardSerializer(locked, context=context).data)

    @extend_schema(
        request=DashboardMemberSerializer(many=True), responses={200: DashboardMemberSerializer(many=True)}
    )
    @action(detail=True, methods=["get", "put"])
    def members(self, request: Request, id=None) -> Response:
        dashboard = self.get_object()
        if request.method == "PUT":
            _require_owner(request, dashboard)
            serializer = DashboardMemberSerializer(data=request.data, many=True)
            serializer.is_valid(raise_exception=True)
            wanted = {item["user_id"]: item["role"] for item in serializer.validated_data}
            allowed = set(
                UserOrganization.objects.filter(
                    organization=dashboard.organization, user_id__in=wanted
                ).values_list("user_id", flat=True)
            )
            if set(wanted) - allowed:
                raise SpecInvalid("Dashboards can only be shared with members of your organization.")
            with transaction.atomic():
                dashboard.members.exclude(user_id__in=wanted).delete()
                for user_id, role in wanted.items():
                    DashboardMember.objects.update_or_create(
                        dashboard=dashboard, user_id=user_id, defaults={"role": role}
                    )
        members = dashboard.members.select_related("user").order_by("user__email")
        return Response(DashboardMemberSerializer(members, many=True).data)

    @extend_schema(request=PersonalViewSerializer, responses={200: PersonalViewSerializer})
    @action(detail=True, methods=["get", "put", "delete"], url_path="personal-view")
    def personal_view(self, request: Request, id=None) -> Response:
        """The viewer's own changes; the author's dashboard is never modified."""
        dashboard = self.get_object()
        view = PersonalView.objects.filter(dashboard=dashboard, user=request.user, is_default=True).first()
        if request.method == "GET":
            if view is None:
                return Response(status=status.HTTP_204_NO_CONTENT)
            return Response(PersonalViewSerializer(view).data)
        if request.method == "DELETE":
            PersonalView.objects.filter(dashboard=dashboard, user=request.user).delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        if not dashboard.spec.get("settings", {}).get("allowPersonalize", True):
            raise PermissionDenied("The author has turned off personalization for this dashboard.")
        serializer = PersonalViewSerializer(view, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            state = dashboard_spec.clean_personal_state(serializer.validated_data.get("state") or {})
        except dashboard_spec.SpecError as exc:
            raise SpecInvalid(str(exc)) from exc
        saved = serializer.save(dashboard=dashboard, user=request.user, state=state, is_default=True)
        return Response(PersonalViewSerializer(saved).data)

    @extend_schema(request=DashboardExportSerializer, responses={200: OpenApiTypes.BINARY})
    @action(detail=True, methods=["post"])
    def export(self, request: Request, id=None) -> HttpResponse:
        """Download the dashboard: Excel (data per visual + raw data), interactive HTML, JSON, or a
        PDF made from page images the browser painted."""
        dashboard = self.get_object()
        serializer = DashboardExportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        name = dashboard.name or "Dashboard"
        try:
            if data["format"] == "json":
                content = json.dumps({"name": name, "spec": dashboard.spec}, indent=2).encode("utf-8")
                return _download(content, mime="application/json", filename=f"{name}.json")
            if data["format"] == "pdf":
                return _download(
                    dashboard_export.pdf(name, data.get("images") or []),
                    mime="application/pdf",
                    filename=f"{name}.pdf",
                )
            spec = dashboard.spec
            wanted = dashboard_spec.dataset_ids(spec) | {
                end["datasetId"]
                for link in spec.get("relationships") or []
                for end in (link["from"], link["to"])
            }
            datasets = datasets_for(request, wanted, dashboard.id)
            pages = dashboard_export.render_pages(spec, datasets, data["mode"], timezone.localdate())
            if data["format"] == "xlsx":
                return _download(
                    dashboard_export.workbook(name, pages, datasets),
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    filename=f"{name}.xlsx",
                )
            page = dashboard_export.interactive_html(name, dashboard.description, pages, data["mode"])
            return _download(page.encode("utf-8"), mime="text/html; charset=utf-8", filename=f"{name}.html")
        except dashboard_export.ExportError as exc:
            raise ExportInvalid(str(exc)) from exc

    @extend_schema(request=AutoDashboardSerializer, responses={201: DashboardSerializer})
    @action(detail=False, methods=["post"])
    def auto(self, request: Request) -> Response:
        """A starter dashboard for a dataset: KPIs, trend, composition, ranking, matrix, slicers."""
        serializer = AutoDashboardSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization = _organization(request)
        require_organization_write_access(request.user, organization.id)
        dataset = (
            datasets_analyzable_by(request.user, organization)
            .filter(id=serializer.validated_data["datasetId"])
            .first()
        )
        if dataset is None:
            raise NotFound("Dataset not found.")
        profile = services.ensure_profile(dataset)
        spec = _clean(autodash.build(str(dataset.id), profile))
        dashboard = Dashboard.objects.create(
            organization=organization,
            owner=request.user,
            name=(serializer.validated_data.get("name") or f"{dataset.name} overview")[:200],
            spec=spec,
        )
        _snapshot(dashboard, request.user, "Created automatically")
        body = DashboardSerializer(
            dashboard, context={"roles": {str(dashboard.id): "owner"}, "request": request}
        ).data
        return Response(body, status=status.HTTP_201_CREATED)


class ThemeViewSet(viewsets.ModelViewSet):
    """Custom dashboard themes; ``builtin/`` lists the built-in ones."""

    permission_classes = [IsAuthenticated]
    serializer_class = ThemeSerializer
    lookup_field = "id"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Theme.objects.none()
        organization = _organization(self.request)
        return (
            Theme.objects.filter(organization=organization)
            .filter(Q(owner=self.request.user) | Q(is_shared=True))
            .order_by("name")
        )

    @extend_schema(responses={200: OpenApiTypes.OBJECT})
    @action(detail=False, methods=["get"])
    def builtin(self, request: Request) -> Response:
        return Response({"themes": themes.public_themes(), "default": themes.DEFAULT_THEME})

    def perform_create(self, serializer):
        organization = _organization(self.request)
        require_organization_write_access(self.request.user, organization.id)
        serializer.save(
            organization=organization,
            owner=self.request.user,
            spec=themes.clean_theme(serializer.validated_data.get("spec")),
        )

    def perform_update(self, serializer):
        if serializer.instance.owner_id != self.request.user.id:
            raise PermissionDenied("Only the theme's owner may change it.")
        extra = (
            {"spec": themes.clean_theme(serializer.validated_data["spec"])}
            if "spec" in serializer.validated_data
            else {}
        )
        serializer.save(**extra)

    def perform_destroy(self, instance: Theme) -> None:
        if instance.owner_id != self.request.user.id:
            raise PermissionDenied("Only the theme's owner may delete it.")
        instance.delete()


class MeasureViewSet(viewsets.ModelViewSet):
    """The measures library: calculations saved once and added to any dashboard."""

    permission_classes = [IsAuthenticated]
    serializer_class = MeasureSerializer
    lookup_field = "id"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Measure.objects.none()
        queryset = Measure.objects.filter(organization=_organization(self.request))
        dataset_id = self.request.query_params.get("datasetId")
        if dataset_id:
            queryset = queryset.filter(Q(dataset_id=dataset_id) | Q(dataset__isnull=True))
        return queryset

    @extend_schema(parameters=[OpenApiParameter("datasetId", str, required=False)])
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    def _check_dataset(self, serializer: Any) -> None:
        dataset_id = serializer.validated_data.get("dataset_id")
        if (
            dataset_id
            and not datasets_visible_to(self.request.user, _organization(self.request))
            .filter(id=dataset_id)
            .exists()
        ):
            raise NotFound("Dataset not found.")

    def perform_create(self, serializer):
        organization = _organization(self.request)
        require_organization_write_access(self.request.user, organization.id)
        self._check_dataset(serializer)
        serializer.save(organization=organization, owner=self.request.user)

    def _require_owner(self, instance: Measure) -> None:
        if instance.owner_id != self.request.user.id and not _is_admin(
            self.request.user, instance.organization
        ):
            raise PermissionDenied("Only the measure's owner may change it.")

    def perform_update(self, serializer):
        self._require_owner(serializer.instance)
        self._check_dataset(serializer)
        serializer.save()

    def perform_destroy(self, instance: Measure) -> None:
        self._require_owner(instance)
        instance.delete()


class VisualsView(APIView):
    """Draw a batch of dashboard visuals with the current filters (Python builds every visual)."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [VisualizeQueryThrottle]

    @extend_schema(request=VisualsRequestSerializer, responses={200: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        serializer = VisualsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            visuals = [
                dashboard_spec.clean_visual(v, f"Visual {i + 1}") for i, v in enumerate(data["visuals"])
            ]
            filters = [dashboard_spec.clean_filter(f, "Filters") for f in data.get("filters") or []]
            relationships = dashboard_spec.clean_relationships(data.get("relationships") or [])
        except dashboard_spec.SpecError as exc:
            raise SpecInvalid(str(exc)) from exc
        datasets = self._datasets(request, visuals, data.get("dashboardId"))
        # Datasets reached only through a relationship are loaded too (their keys answer filters).
        linked = {end["datasetId"] for r in relationships for end in (r["from"], r["to"])} - set(datasets)
        if linked:
            datasets.update(
                self._datasets(request, [{"data": {"datasetId": i}} for i in linked], data.get("dashboardId"))
            )
        theme = data.get("theme") or themes.DEFAULT_THEME
        today = data.get("today") or timezone.localdate()
        context = {
            "parameters": {str(k)[:60]: v for k, v in list((data.get("parameters") or {}).items())[:50]},
            "calculations": {
                str(c.get("name"))[:60]: str(c.get("expression") or "")[:1000]
                for c in data.get("calculations") or []
                if isinstance(c, dict) and c.get("name")
            },
            "relationships": relationships,
        }
        return Response(dashboards.render(datasets, visuals, filters, theme, data["mode"], today, context))

    def _datasets(
        self, request: Request, visuals: list[dict[str, Any]], dashboard_id: Any
    ) -> dict[str, Dataset]:
        wanted = {v["data"]["datasetId"] for v in visuals if v["data"].get("datasetId")}
        return datasets_for(request, wanted, dashboard_id)
