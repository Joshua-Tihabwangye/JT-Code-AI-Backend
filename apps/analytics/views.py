from __future__ import annotations

import hashlib

from django.db import transaction
from rest_framework import serializers, status, viewsets
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
from apps.analytics.serializers import (
    AnalysisRunSerializer,
    DatasetCreateSerializer,
    DatasetGrantSerializer,
    DatasetSerializer,
    DatasetUpdateSerializer,
    VisualizationSerializer,
)
from apps.analytics.services import AnalysisError, dataframe_from_bytes, profile_frame
from apps.analytics.tasks import execute_analysis_run, execute_visualization
from apps.assets.models import Asset
from apps.assets.services import soft_delete_asset
from apps.core.throttling import AnalyticsThrottle, BurstThrottle
from apps.identity.authorization import (
    organization_for_request,
    require_organization_write_access,
)


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
        inline_data = serializer.validated_data.get("inline_data", "")
        asset = serializer.validated_data.get("asset")
        mime_type = serializer.validated_data.get("mime_type", "text/csv")
        schema: dict = {}
        row_count = 0
        byte_size = 0
        checksum = ""
        if inline_data:
            content = inline_data.encode("utf-8")
            from django.conf import settings

            if len(content) > settings.ANALYTICS_MAX_INLINE_BYTES:
                raise serializers.ValidationError(
                    {"inline_data": "Inline datasets exceed the configured byte limit."}
                )
            try:
                frame = dataframe_from_bytes(content, mime_type=mime_type)
            except AnalysisError as exc:
                raise serializers.ValidationError({"inline_data": str(exc)}) from exc
            profile = profile_frame(frame)
            schema = {"columns": profile["columns"], "dtypes": profile["dtypes"]}
            row_count = profile["rows"]
            byte_size = len(content)
            checksum = hashlib.sha256(content).hexdigest()
        elif asset:
            if asset.organization_id != organization.id:
                raise PermissionDenied("Asset is outside the selected organization.")
            if asset.status != Asset.Status.READY:
                raise serializers.ValidationError({"asset": "The asset is not ready."})
            if not asset.imagekit_file_path or not asset.checksum_sha256:
                raise serializers.ValidationError(
                    {"asset": "The asset has not passed integrity verification."}
                )
            from django.conf import settings

            asset_mime = str(asset.metadata.get("content_type") or "").split(";", 1)[0].lower()
            if asset_mime and asset_mime != mime_type:
                raise serializers.ValidationError(
                    {"asset": "The asset content type does not match the dataset MIME type."}
                )
            if asset.bytes > settings.ANALYTICS_MAX_DATASET_BYTES:
                raise serializers.ValidationError({"asset": "The asset exceeds the dataset byte limit."})
            byte_size = asset.bytes
            checksum = asset.checksum_sha256
        dataset = serializer.save(
            owner=request.user,
            organization=organization,
            schema=schema,
            row_count=row_count,
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
        for asset in [*result_assets, *chart_assets]:
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


class AnalysisRunViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = AnalysisRunSerializer
    lookup_field = "id"
    http_method_names = ["get", "post", "head", "options"]

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
        run = serializer.save(owner=request.user, dataset=dataset)
        execute_analysis_run.delay(str(run.id))
        return Response(AnalysisRunSerializer(run).data, status=status.HTTP_202_ACCEPTED)


class VisualizationViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = VisualizationSerializer
    lookup_field = "id"
    http_method_names = ["get", "post", "head", "options"]

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
        execute_visualization.delay(str(visualization.id))
        return Response(VisualizationSerializer(visualization).data, status=status.HTTP_202_ACCEPTED)
