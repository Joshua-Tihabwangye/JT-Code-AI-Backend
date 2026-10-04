"""Analysis and visualization workers.

Each task claims its record under a row lock (``FOR UPDATE OF`` the record only,
so optional related rows can be joined), fetches and verifies source bytes,
runs all pandas/Plotly/Matplotlib work in the isolated engine process, stores
artifacts privately in ImageKit, and validates the result against the
versioned result schema before saving it.
"""

from __future__ import annotations

import base64
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)
RESULT_SCHEMA_VERSION = "1"


def _claim_analysis(run_id: str):
    from apps.analytics.models import AnalysisRun

    with transaction.atomic():
        run = (
            AnalysisRun.objects.select_for_update(of=("self",))
            .select_related("dataset__asset", "owner", "dataset__organization")
            .filter(id=run_id, status=AnalysisRun.Status.QUEUED)
            .first()
        )
        if run is None:
            return None
        run.status = AnalysisRun.Status.RUNNING
        run.started_at = timezone.now()
        run.error_message = ""
        run.save(update_fields=["status", "started_at", "error_message", "updated_at"])
        return run


def _fail(record, message: str, assets) -> None:
    from apps.assets.services import soft_delete_asset

    for asset in assets:
        soft_delete_asset(asset)
    record.status = record.Status.FAILED
    record.error_message = message[:2000]
    record.completed_at = timezone.now()
    record.save(update_fields=["status", "error_message", "completed_at", "updated_at"])


@shared_task(
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=settings.ANALYTICS_TASK_SOFT_TIME_LIMIT_SECONDS,
    time_limit=settings.ANALYTICS_TASK_TIME_LIMIT_SECONDS,
)
def execute_analysis_run(run_id: str) -> None:
    from apps.analytics.models import AnalysisRun
    from apps.analytics.sandbox import run_engine
    from apps.analytics.serializers import AnalysisResultSchemaSerializer
    from apps.analytics.services import AnalysisError, dataset_bytes
    from apps.assets.services import register_generated_asset

    run = _claim_analysis(run_id)
    if run is None:
        return
    result_asset = None
    try:
        dataset = run.dataset
        output = run_engine(
            "analyze", dataset_bytes(dataset), mime_type=dataset.mime_type, transform=run.transform
        )
        source_profile = output["sourceProfile"]
        dataset.schema = {"columns": source_profile["columns"], "dtypes": source_profile["dtypes"]}
        dataset.profile = source_profile
        dataset.row_count = source_profile["rows"]
        if dataset.asset_id:
            dataset.byte_size = dataset.asset.bytes
            dataset.source_checksum_sha256 = dataset.asset.checksum_sha256
        dataset.save(
            update_fields=[
                "schema",
                "profile",
                "row_count",
                "byte_size",
                "source_checksum_sha256",
                "updated_at",
            ]
        )

        result = base64.b64decode(output["result"])
        result_asset = register_generated_asset(
            result,
            owner=run.owner,
            organization=dataset.organization,
            file_name=f"analysis-{run.id}.csv",
            kind="analytics/results",
            content_type="text/csv",
            provenance={
                "kind": "analytics-result",
                "analysis_run_id": str(run.id),
                "dataset_id": str(dataset.id),
            },
        )
        profile = output["profile"]
        schema = AnalysisResultSchemaSerializer(
            data={
                "version": RESULT_SCHEMA_VERSION,
                "format": "csv",
                "columns": profile["columns"],
                "dtypes": profile["dtypes"],
                "rows": profile["rows"],
                "bytes": len(result),
                "checksumSha256": result_asset.checksum_sha256,
            }
        )
        schema.is_valid(raise_exception=True)
        run.profile = profile
        run.result_schema = schema.validated_data
        run.result_preview = output["preview"]
        run.result_asset = result_asset
        run.status = AnalysisRun.Status.COMPLETED
        run.completed_at = timezone.now()
        run.save(
            update_fields=[
                "profile",
                "result_schema",
                "result_preview",
                "result_asset",
                "status",
                "completed_at",
                "updated_at",
            ]
        )
    except AnalysisError as exc:
        _fail(run, str(exc), [result_asset])
    except Exception:
        logger.exception("Unexpected analysis worker failure", extra={"analysis_run_id": run_id})
        _fail(run, "The analysis worker failed. Retry the analysis run.", [result_asset])


def _claim_visualization(visualization_id: str):
    from apps.analytics.models import AnalysisRun, Visualization

    with transaction.atomic():
        visualization = (
            Visualization.objects.select_for_update(of=("self",))
            .select_related(
                "analysis_run__result_asset",
                "analysis_run__owner",
                "analysis_run__dataset__organization",
            )
            .filter(id=visualization_id, status=Visualization.Status.QUEUED)
            .first()
        )
        if visualization is None or visualization.analysis_run.status != AnalysisRun.Status.COMPLETED:
            return None
        visualization.status = Visualization.Status.RUNNING
        visualization.started_at = timezone.now()
        visualization.error_message = ""
        visualization.save(update_fields=["status", "started_at", "error_message", "updated_at"])
        return visualization


@shared_task(
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=settings.ANALYTICS_TASK_SOFT_TIME_LIMIT_SECONDS,
    time_limit=settings.ANALYTICS_TASK_TIME_LIMIT_SECONDS,
)
def execute_visualization(visualization_id: str) -> None:
    import json

    from apps.analytics.models import Visualization
    from apps.analytics.sandbox import run_engine
    from apps.analytics.serializers import VisualizationResultSchemaSerializer
    from apps.analytics.services import AnalysisError, result_bytes, sha256
    from apps.assets.services import register_generated_asset

    visualization = _claim_visualization(visualization_id)
    if visualization is None:
        return
    artifact = spec_asset = None
    try:
        run = visualization.analysis_run
        organization = run.dataset.organization
        output = run_engine(
            "chart",
            result_bytes(run),
            mime_type="text/csv",
            kind=visualization.kind,
            x=visualization.x_column,
            y=visualization.y_column,
            title=visualization.title,
            color=visualization.color_column,
        )
        png = base64.b64decode(output["png"])
        spec_bytes = output["spec"].encode("utf-8")
        provenance = {
            "kind": "analytics-visualization",
            "visualization_id": str(visualization.id),
            "analysis_run_id": str(run.id),
        }
        artifact = register_generated_asset(
            png,
            owner=run.owner,
            organization=organization,
            file_name=f"visualization-{visualization.id}.png",
            kind="analytics/charts",
            content_type="image/png",
            provenance=provenance,
        )
        spec_asset = register_generated_asset(
            spec_bytes,
            owner=run.owner,
            organization=organization,
            file_name=f"visualization-{visualization.id}.plotly.json",
            kind="analytics/charts",
            content_type="application/json",
            provenance={**provenance, "kind": "analytics-visualization-spec"},
        )
        schema = VisualizationResultSchemaSerializer(
            data={
                "version": RESULT_SCHEMA_VERSION,
                "kind": visualization.kind,
                "staticFormat": "png",
                "interactiveFormat": "plotly-json",
                "bytes": len(png),
                "specBytes": len(spec_bytes),
                "points": output["points"],
                "checksumSha256": sha256(png),
                "specChecksumSha256": sha256(spec_bytes),
            }
        )
        schema.is_valid(raise_exception=True)
        visualization.plotly_spec = (
            json.loads(spec_bytes) if len(spec_bytes) <= settings.ANALYTICS_INLINE_SPEC_BYTES else {}
        )
        visualization.artifact_checksum_sha256 = sha256(png)
        visualization.artifact_asset = artifact
        visualization.spec_asset = spec_asset
        visualization.result_schema = schema.validated_data
        visualization.status = Visualization.Status.READY
        visualization.completed_at = timezone.now()
        visualization.save(
            update_fields=[
                "plotly_spec",
                "artifact_checksum_sha256",
                "artifact_asset",
                "spec_asset",
                "result_schema",
                "status",
                "completed_at",
                "updated_at",
            ]
        )
    except AnalysisError as exc:
        _fail(visualization, str(exc), [artifact, spec_asset])
    except Exception:
        logger.exception(
            "Unexpected visualization worker failure", extra={"visualization_id": visualization_id}
        )
        _fail(
            visualization, "The visualization worker failed. Retry the visualization.", [artifact, spec_asset]
        )


@shared_task
def recover_stalled_analytics() -> dict[str, int]:
    """Close durable RUNNING records left behind by killed workers."""
    from apps.analytics.models import AnalysisRun, Visualization
    from apps.assets.models import Asset
    from apps.assets.services import soft_delete_asset

    cutoff = timezone.now() - timedelta(minutes=settings.ANALYTICS_STALLED_AFTER_MINUTES)
    now = timezone.now()
    analyses = AnalysisRun.objects.filter(status=AnalysisRun.Status.RUNNING, started_at__lt=cutoff).update(
        status=AnalysisRun.Status.FAILED,
        error_message="The analysis worker stopped before completion.",
        completed_at=now,
        updated_at=now,
    )
    visualizations = Visualization.objects.filter(
        status=Visualization.Status.RUNNING, started_at__lt=cutoff
    ).update(
        status=Visualization.Status.FAILED,
        error_message="The visualization worker stopped before completion.",
        completed_at=now,
        updated_at=now,
    )
    # A hard-killed process can stop after provider registration but before
    # linking the asset. Provenance makes those rows recoverable without ever
    # treating user-supplied source assets as garbage.
    orphan_cutoff = now - timedelta(hours=settings.ASSET_ORPHAN_GRACE_HOURS)
    candidates = Asset.objects.filter(status=Asset.Status.READY, created_at__lt=orphan_cutoff)
    orphans = [
        *candidates.filter(provenance__kind="analytics-result", analysis_results__isnull=True),
        *candidates.filter(provenance__kind="analytics-visualization", analytics_visualizations__isnull=True),
        *candidates.filter(provenance__kind="analytics-visualization-spec", analytics_specs__isnull=True),
    ]
    for asset in orphans:
        soft_delete_asset(asset)
    return {"analysis_runs": analyses, "visualizations": visualizations, "orphan_assets": len(orphans)}
