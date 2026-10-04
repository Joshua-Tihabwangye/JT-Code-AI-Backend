from __future__ import annotations

import hashlib

from django.conf import settings
from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.analytics.access import (
    analysis_runs_visible_to,
    can_manage_dataset,
    datasets_analyzable_by,
    datasets_manageable_by,
    datasets_visible_to,
    visualizations_visible_to,
)
from apps.analytics.models import AnalysisRun, Dataset, DatasetGrant, Visualization
from apps.analytics.sandbox import run_engine
from apps.analytics.serializers import (
    AnalysisRunSerializer,
    DatasetCreateSerializer,
    DatasetGrantSerializer,
    DatasetSerializer,
    DatasetUpdateSerializer,
    VisualizationSerializer,
)
from apps.analytics.services import AnalysisError
from apps.analytics.tasks import execute_analysis_run, execute_visualization
from apps.assets.access import assets_visible_to
from apps.assets.models import Asset
from apps.assets.services import soft_delete_asset
from apps.core.throttling import AnalyticsThrottle, BurstThrottle
from apps.identity.authorization import (
    organization_for_request,
    require_organization_write_access,
)
from apps.usage import services as metering
from apps.usage.concurrency import enforce_concurrency
from apps.usage.models import Feature


def _organization(view):
    if getattr(view, "swagger_fake_view", False):
        return None
    return organization_for_request(view.request, required=True)


class DatasetViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    lookup_field = "id"

    def get_queryset(self):
        organization = _organization(self)
        if organization is None:
            return Dataset.objects.none()
        return (
            datasets_visible_to(self.request.user, organization)
            .select_related("asset", "owner")
            .prefetch_related("grants")
        )

    def get_serializer_class(self):
        if self.action == "create":
            return DatasetCreateSerializer
        if self.action in {"update", "partial_update"}:
            return DatasetUpdateSerializer
        return DatasetSerializer

    def create(self, request: Request, *args, **kwargs):
        organization = organization_for_request(request, required=True)
        require_organization_write_access(request.user, organization.id)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        inline_data = serializer.validated_data.pop("inline_data", "")
        asset_id = serializer.validated_data.pop("asset", None)
        mime_type = serializer.validated_data.get("mime_type", "text/csv")
        asset = None
        if inline_data:
            content = inline_data.encode("utf-8")
            if len(content) > settings.ANALYTICS_MAX_INLINE_BYTES:
                raise serializers.ValidationError(
                    {"inline_data": "Inline datasets exceed the configured byte limit."}
                )
            try:
                profile = run_engine("profile", content, mime_type=mime_type)["profile"]
            except AnalysisError as exc:
                raise serializers.ValidationError({"inline_data": str(exc)}) from exc
            byte_size, checksum = len(content), hashlib.sha256(content).hexdigest()
        else:
            # The caller must be able to read the asset itself; organization
            # membership alone would let a dataset expose someone's private file.
            asset = assets_visible_to(request.user, organization.id).filter(id=asset_id).first()
            if asset is None:
                raise serializers.ValidationError({"asset": "The asset was not found."})
            if asset.status != Asset.Status.READY:
                raise serializers.ValidationError({"asset": "The asset is not ready."})
            if not asset.imagekit_file_path or not asset.checksum_sha256:
                raise serializers.ValidationError(
                    {"asset": "The asset has not passed integrity verification."}
                )
            asset_mime = asset.content_type.split(";", 1)[0].lower()
            if asset_mime not in {mime_type, "application/octet-stream"}:
                raise serializers.ValidationError(
                    {"asset": "The asset content type does not match the dataset MIME type."}
                )
            if asset.bytes > settings.ANALYTICS_MAX_DATASET_BYTES:
                raise serializers.ValidationError({"asset": "The asset exceeds the dataset byte limit."})
            profile, byte_size, checksum = {}, asset.bytes, asset.checksum_sha256
        dataset = serializer.save(
            owner=request.user,
            organization=organization,
            asset=asset,
            inline_data=inline_data,
            schema={"columns": profile.get("columns", []), "dtypes": profile.get("dtypes", {})}
            if profile
            else {},
            profile=profile,
            row_count=profile.get("rows", 0),
            byte_size=byte_size,
            source_checksum_sha256=checksum,
        )
        return Response(DatasetSerializer(dataset).data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        if not can_manage_dataset(self.request.user, self.get_object()):
            raise PermissionDenied("Only the dataset owner or an organization admin may update it.")
        serializer.save()

    @transaction.atomic
    def perform_destroy(self, instance):
        if not can_manage_dataset(self.request.user, instance):
            raise PermissionDenied("Only the dataset owner or an organization admin may delete it.")
        result_assets = Asset.objects.filter(analysis_results__dataset=instance).distinct()
        chart_assets = Asset.objects.filter(
            analytics_visualizations__analysis_run__dataset=instance
        ).distinct()
        spec_assets = Asset.objects.filter(analytics_specs__analysis_run__dataset=instance).distinct()
        for asset in [*result_assets, *chart_assets, *spec_assets]:
            soft_delete_asset(asset)
        instance.delete()


class DatasetGrantViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = DatasetGrantSerializer
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        organization = _organization(self)
        if organization is None:
            return DatasetGrant.objects.none()
        return DatasetGrant.objects.filter(
            dataset__in=datasets_manageable_by(self.request.user, organization)
        ).select_related("dataset", "user")

    def _validate_grant(self, serializer, instance=None):
        organization = organization_for_request(self.request, required=True)
        dataset = serializer.validated_data.get("dataset", getattr(instance, "dataset", None))
        user = serializer.validated_data.get("user", getattr(instance, "user", None))
        if dataset is None or dataset.organization_id != organization.id:
            raise PermissionDenied("Dataset is outside the selected organization.")
        if not can_manage_dataset(self.request.user, dataset):
            raise PermissionDenied("Only the dataset owner or an organization admin may manage grants.")
        if user is None or not user.organizations.filter(id=organization.id).exists():
            raise serializers.ValidationError({"user": "The grantee must belong to this organization."})
        if user.id == dataset.owner_id:
            raise serializers.ValidationError({"user": "The dataset owner does not require a grant."})

    def perform_create(self, serializer):
        self._validate_grant(serializer)
        serializer.save()


def _reserve_run(run: AnalysisRun, user) -> None:
    metering.reserve(
        organization=run.dataset.organization,
        user=user,
        feature=Feature.ANALYSIS_RUNS,
        source_type="analysis_run",
        source_id=run.id,
    )


def _can_manage_run(user, run: AnalysisRun) -> bool:
    return run.owner_id == user.id or can_manage_dataset(user, run.dataset)


def _delete_run_artifacts(run: AnalysisRun) -> None:
    assets = [run.result_asset]
    for visualization in run.visualizations.all():
        assets.extend([visualization.artifact_asset, visualization.spec_asset])
    for asset in assets:
        soft_delete_asset(asset)


class AnalysisRunViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = AnalysisRunSerializer
    lookup_field = "id"
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_throttles(self):
        return [AnalyticsThrottle(), BurstThrottle()] if self.action == "create" else []

    def get_queryset(self):
        organization = _organization(self)
        if organization is None:
            return AnalysisRun.objects.none()
        return analysis_runs_visible_to(self.request.user, organization).select_related(
            "dataset", "result_asset"
        )

    def create(self, request: Request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization = organization_for_request(request, required=True)
        dataset = (
            datasets_analyzable_by(request.user, organization)
            .filter(id=serializer.validated_data["dataset"].id)
            .first()
        )
        if dataset is None:
            return Response({"detail": "Dataset not found."}, status=status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            enforce_concurrency(organization, "analysis_runs")
            run = serializer.save(owner=request.user, dataset=dataset)
            _reserve_run(run, request.user)
        transaction.on_commit(lambda: execute_analysis_run.delay(str(run.id)))
        return Response(AnalysisRunSerializer(run).data, status=status.HTTP_202_ACCEPTED)

    @transaction.atomic
    def perform_destroy(self, instance):
        if not _can_manage_run(self.request.user, instance):
            raise PermissionDenied("Only the run owner, dataset owner or an admin may delete it.")
        _delete_run_artifacts(instance)
        instance.delete()

    @extend_schema(request=None, responses={202: AnalysisRunSerializer})
    @action(detail=True, methods=["post"])
    def retry(self, request: Request, id=None):
        """Queue a new run with the same transform after a failure."""
        run = self.get_object()
        if run.status != AnalysisRun.Status.FAILED:
            return Response({"detail": "Only failed runs can be retried."}, status=status.HTTP_409_CONFLICT)
        organization = organization_for_request(request, required=True)
        if not datasets_analyzable_by(request.user, organization).filter(id=run.dataset_id).exists():
            raise PermissionDenied("You may not analyze this dataset.")
        with transaction.atomic():
            enforce_concurrency(organization, "analysis_runs")
            retried = AnalysisRun.objects.create(
                dataset=run.dataset, owner=request.user, transform=run.transform
            )
            _reserve_run(retried, request.user)
        transaction.on_commit(lambda: execute_analysis_run.delay(str(retried.id)))
        return Response(AnalysisRunSerializer(retried).data, status=status.HTTP_202_ACCEPTED)

    @extend_schema(responses={(200, "text/csv"): bytes})
    @action(detail=True, methods=["get"])
    def download(self, request: Request, id=None):
        """Stream the result CSV through the API (run visibility applies)."""
        from django.http import StreamingHttpResponse

        from apps.assets.imagekit import stream_file

        run = self.get_object()
        asset = run.result_asset
        if run.status != AnalysisRun.Status.COMPLETED or asset is None or asset.status != Asset.Status.READY:
            return Response({"detail": "The result is not available."}, status=status.HTTP_404_NOT_FOUND)
        response = StreamingHttpResponse(stream_file(asset.imagekit_file_path), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="analysis-{run.id}.csv"'
        response["X-Content-Type-Options"] = "nosniff"
        return response


class VisualizationViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = VisualizationSerializer
    lookup_field = "id"
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_throttles(self):
        return [AnalyticsThrottle(), BurstThrottle()] if self.action == "create" else []

    def get_queryset(self):
        organization = _organization(self)
        if organization is None:
            return Visualization.objects.none()
        return visualizations_visible_to(self.request.user, organization).select_related(
            "analysis_run", "artifact_asset"
        )

    def create(self, request: Request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization = organization_for_request(request, required=True)
        run = (
            analysis_runs_visible_to(request.user, organization)
            .filter(id=serializer.validated_data["analysis_run"].id)
            .first()
        )
        if run is None:
            return Response({"detail": "Analysis run not found."}, status=status.HTTP_404_NOT_FOUND)
        if run.status != AnalysisRun.Status.COMPLETED:
            return Response({"detail": "Analysis run is not complete."}, status=status.HTTP_409_CONFLICT)
        visualization = serializer.save(analysis_run=run)
        transaction.on_commit(lambda: execute_visualization.delay(str(visualization.id)))
        return Response(VisualizationSerializer(visualization).data, status=status.HTTP_202_ACCEPTED)

    @transaction.atomic
    def perform_destroy(self, instance):
        if not _can_manage_run(self.request.user, instance.analysis_run):
            raise PermissionDenied("Only the run owner, dataset owner or an admin may delete it.")
        soft_delete_asset(instance.artifact_asset)
        soft_delete_asset(instance.spec_asset)
        instance.delete()

    @extend_schema(request=None, responses={202: VisualizationSerializer})
    @action(detail=True, methods=["post"])
    def retry(self, request: Request, id=None):
        visualization = self.get_object()
        if visualization.status != Visualization.Status.FAILED:
            return Response({"detail": "Only failed visualizations can be retried."}, status=409)
        Visualization.objects.filter(id=visualization.id).update(
            status=Visualization.Status.QUEUED, error_message=""
        )
        transaction.on_commit(lambda: execute_visualization.delay(str(visualization.id)))
        visualization.refresh_from_db()
        return Response(VisualizationSerializer(visualization).data, status=status.HTTP_202_ACCEPTED)
