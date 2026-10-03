from __future__ import annotations

import json
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def _claim_analysis(run_id: str):
    from apps.analytics.models import AnalysisRun

    with transaction.atomic():
        run = (
            AnalysisRun.objects.select_for_update()
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


@shared_task(
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=settings.ANALYTICS_TASK_SOFT_TIME_LIMIT_SECONDS,
    time_limit=settings.ANALYTICS_TASK_TIME_LIMIT_SECONDS,
)
def execute_analysis_run(run_id: str) -> None:
    from apps.analytics.models import AnalysisRun
    from apps.analytics.services import (
        AnalysisError,
        apply_transform,
        dataframe_for_dataset,
        frame_as_csv,
        frame_preview,
        profile_frame,
    )
    from apps.assets.services import register_generated_asset, soft_delete_asset

    run = _claim_analysis(run_id)
    if run is None:
        return
    result_asset = None
    try:
        source_frame = dataframe_for_dataset(run.dataset)
        source_profile = profile_frame(source_frame)
        run.dataset.schema = {
            "columns": source_profile["columns"],
            "dtypes": source_profile["dtypes"],
        }
        run.dataset.row_count = source_profile["rows"]
        if run.dataset.asset_id:
            run.dataset.byte_size = run.dataset.asset.bytes
            run.dataset.source_checksum_sha256 = run.dataset.asset.checksum_sha256
        run.dataset.save(
            update_fields=[
                "schema",
                "row_count",
                "byte_size",
                "source_checksum_sha256",
                "updated_at",
            ]
        )

        frame = apply_transform(source_frame, run.transform)
        result_bytes = frame_as_csv(frame)
        if not result_bytes or len(result_bytes) > settings.ANALYTICS_MAX_RESULT_BYTES:
            raise AnalysisError("The analysis result exceeds the configured artifact limit.")
        result_asset = register_generated_asset(
            result_bytes,
            owner=run.owner,
            organization=run.dataset.organization,
            file_name=f"analysis-{run.id}.csv",
            folder=f"/jt-code/analytics/{run.dataset.organization_id}/results",
            content_type="text/csv",
            provenance={
                "kind": "analytics-result",
                "analysis_run_id": str(run.id),
                "dataset_id": str(run.dataset_id),
            },
        )
        run.profile = profile_frame(frame)
        run.result_schema = {
            "version": "1",
            "format": "csv",
            "columns": run.profile["columns"],
            "dtypes": run.profile["dtypes"],
            "rows": run.profile["rows"],
            "bytes": len(result_bytes),
        }
        run.result_preview = frame_preview(frame)
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
        soft_delete_asset(result_asset)
        run.status = AnalysisRun.Status.FAILED
        run.error_message = str(exc)[:2000]
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_message", "completed_at", "updated_at"])
    except Exception:
        logger.exception("Unexpected analysis worker failure", extra={"analysis_run_id": run_id})
        soft_delete_asset(result_asset)
        run.status = AnalysisRun.Status.FAILED
        run.error_message = "The analysis worker failed. Retry with a new analysis run."
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_message", "completed_at", "updated_at"])


def _claim_visualization(visualization_id: str):
    from apps.analytics.models import AnalysisRun, Visualization

    with transaction.atomic():
        visualization = (
            Visualization.objects.select_for_update()
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
    from apps.analytics.models import Visualization
    from apps.analytics.services import AnalysisError, chart, dataframe_for_result, sha256
    from apps.assets.services import register_generated_asset, soft_delete_asset

    visualization = _claim_visualization(visualization_id)
    if visualization is None:
        return
    artifact = None
    try:
        frame = dataframe_for_result(visualization.analysis_run)
        spec, png = chart(
            frame,
            kind=visualization.kind,
            x=visualization.x_column,
            y=visualization.y_column,
        )
        if (
            len(json.dumps(spec, separators=(",", ":")).encode("utf-8"))
            > settings.ANALYTICS_MAX_PLOTLY_SPEC_BYTES
        ):
            raise AnalysisError("The interactive chart specification exceeds the configured limit.")
        artifact = register_generated_asset(
            png,
            owner=visualization.analysis_run.owner,
            organization=visualization.analysis_run.dataset.organization,
            file_name=f"visualization-{visualization.id}.png",
            folder=f"/jt-code/analytics/{visualization.analysis_run.dataset.organization_id}/charts",
            content_type="image/png",
            provenance={
                "kind": "analytics-visualization",
                "visualization_id": str(visualization.id),
                "analysis_run_id": str(visualization.analysis_run_id),
            },
        )
        visualization.plotly_spec = spec
        visualization.artifact_checksum_sha256 = sha256(png)
        visualization.artifact_asset = artifact
        visualization.result_schema = {
            "version": "1",
            "static_format": "png",
            "interactive_format": "plotly-json",
            "bytes": len(png),
            "points": len(frame.index),
        }
        visualization.status = Visualization.Status.READY
        visualization.completed_at = timezone.now()
        visualization.save(
            update_fields=[
                "plotly_spec",
                "artifact_checksum_sha256",
                "artifact_asset",
                "result_schema",
                "status",
                "completed_at",
                "updated_at",
            ]
        )
    except AnalysisError as exc:
        soft_delete_asset(artifact)
        visualization.status = Visualization.Status.FAILED
        visualization.error_message = str(exc)[:2000]
        visualization.completed_at = timezone.now()
        visualization.save(update_fields=["status", "error_message", "completed_at", "updated_at"])
    except Exception:
        logger.exception(
            "Unexpected visualization worker failure",
            extra={"visualization_id": visualization_id},
        )
        soft_delete_asset(artifact)
        visualization.status = Visualization.Status.FAILED
        visualization.error_message = "The visualization worker failed. Create a new visualization to retry."
        visualization.completed_at = timezone.now()
        visualization.save(update_fields=["status", "error_message", "completed_at", "updated_at"])


@shared_task
def recover_stalled_analytics() -> dict[str, int]:
    """Close durable RUNNING records left behind by killed workers."""
    from apps.analytics.models import AnalysisRun, Visualization
    from apps.assets.models import Asset
    from apps.assets.services import soft_delete_asset

    cutoff = timezone.now() - timedelta(minutes=settings.ANALYTICS_STALLED_AFTER_MINUTES)
    now = timezone.now()
    analyses = AnalysisRun.objects.filter(
        status=AnalysisRun.Status.RUNNING,
        started_at__lt=cutoff,
    ).update(
        status=AnalysisRun.Status.FAILED,
        error_message="The analysis worker stopped before completion.",
        completed_at=now,
        updated_at=now,
    )
    visualizations = Visualization.objects.filter(
        status=Visualization.Status.RUNNING,
        started_at__lt=cutoff,
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
    orphan_results = Asset.objects.filter(
        status=Asset.Status.READY,
        provenance__kind="analytics-result",
        analysis_results__isnull=True,
        created_at__lt=orphan_cutoff,
    )
    orphan_charts = Asset.objects.filter(
        status=Asset.Status.READY,
        provenance__kind="analytics-visualization",
        analytics_visualizations__isnull=True,
        created_at__lt=orphan_cutoff,
    )
    orphan_assets = 0
    for asset in [*orphan_results, *orphan_charts]:
        soft_delete_asset(asset)
        orphan_assets += 1
    return {
        "analysis_runs": analyses,
        "visualizations": visualizations,
        "orphan_assets": orphan_assets,
    }
