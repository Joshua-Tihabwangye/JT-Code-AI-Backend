from __future__ import annotations

import ast
import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.utils import timezone

from apps.analytics.access import datasets_analyzable_by, datasets_visible_to
from apps.analytics.models import AnalysisRun, Dataset, DatasetGrant, Visualization
from apps.analytics.services import AnalysisError, apply_transform, bytes_for_asset, dataframe_from_bytes
from apps.analytics.tasks import (
    execute_analysis_run,
    execute_visualization,
    recover_stalled_analytics,
)
from apps.assets.models import Asset
from apps.identity.models import Organization, Role, UserOrganization, UserRole

pytestmark = pytest.mark.django_db


def make_member(django_user_model, organization, label: str, role: str):
    person = django_user_model.objects.create_user(
        username=label,
        supabase_user_id=f"supabase-{label}",
        email=f"{label}@example.test",
    )
    UserOrganization.objects.create(user=person, organization=organization)
    role_record, _ = Role.objects.get_or_create(name=role)
    UserRole.objects.filter(user=person, organization=organization).exclude(role=role_record).delete()
    UserRole.objects.get_or_create(user=person, role=role_record, organization=organization)
    return person


@pytest.fixture
def analytics_org(django_user_model):
    organization = Organization.objects.create(name="Analytics Org")
    owner = make_member(django_user_model, organization, "dataset-owner", Role.RoleType.EDITOR)
    organization.owner = owner
    organization.save(update_fields=["owner"])
    viewer = make_member(django_user_model, organization, "dataset-viewer", Role.RoleType.VIEWER)
    analyst = make_member(django_user_model, organization, "dataset-analyst", Role.RoleType.VIEWER)
    return organization, owner, viewer, analyst


def make_dataset(organization, owner, **overrides):
    values = {
        "organization": organization,
        "owner": owner,
        "name": "Revenue",
        "inline_data": "region,revenue\nEast,20\nWest,10\nEast,30\n",
        "mime_type": "text/csv",
        "byte_size": 47,
    }
    values.update(overrides)
    return Dataset.objects.create(**values)


def make_asset(organization, owner, label: str, **overrides):
    values = {
        "organization": organization,
        "owner": owner,
        "imagekit_file_id": f"file-{label}",
        "imagekit_file_path": f"/jt-code/{label}.csv",
        "secure_url": f"https://ik.example.test/jt-code/{label}.csv",
        "resource_type": "raw",
        "format": "csv",
        "bytes": 10,
        "original_filename": f"{label}.csv",
        "status": Asset.Status.READY,
        "checksum_sha256": "a" * 64,
    }
    values.update(overrides)
    return Asset.objects.create(**values)


def test_dataset_acl_distinguishes_view_and_analyze_grants(analytics_org):
    organization, owner, viewer, analyst = analytics_org
    private = make_dataset(organization, owner)
    shared = make_dataset(organization, owner, name="Shared", is_shared=True)
    DatasetGrant.objects.create(dataset=private, user=viewer, permission=DatasetGrant.Permission.VIEW)
    DatasetGrant.objects.create(dataset=private, user=analyst, permission=DatasetGrant.Permission.ANALYZE)

    assert set(datasets_visible_to(viewer, organization)) == {private, shared}
    assert set(datasets_analyzable_by(viewer, organization)) == {shared}
    assert set(datasets_analyzable_by(analyst, organization)) == {private, shared}


def test_cross_tenant_datasets_and_results_are_invisible(api_client, analytics_org, django_user_model):
    organization, owner, _viewer, _analyst = analytics_org
    dataset = make_dataset(organization, owner)
    run = AnalysisRun.objects.create(dataset=dataset, owner=owner)
    other_org = Organization.objects.create(name="Other")
    outsider = make_member(django_user_model, other_org, "outsider-analytics", Role.RoleType.ADMIN)
    api_client.force_authenticate(outsider)

    assert api_client.get("/api/v1/analysis/datasets/").json().get("count", 0) == 0
    assert api_client.get(f"/api/v1/analysis/datasets/{dataset.id}/").status_code == 404
    assert api_client.get(f"/api/v1/analysis/runs/{run.id}/").status_code == 404


def test_dataset_api_validates_source_and_never_echoes_inline_data(api_client, analytics_org, monkeypatch):
    organization, owner, _viewer, _analyst = analytics_org
    api_client.force_authenticate(owner)
    response = api_client.post(
        "/api/v1/analysis/datasets/",
        {"name": "Sales", "mime_type": "text/csv", "inline_data": "x,y\na,1\n"},
        format="json",
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )
    assert response.status_code == 201
    assert "inline_data" not in response.json()
    assert response.json()["schema"]["columns"] == ["x", "y"]

    monkeypatch.setattr(settings, "ANALYTICS_MAX_INLINE_BYTES", 3)
    oversized = api_client.post(
        "/api/v1/analysis/datasets/",
        {"name": "Large", "mime_type": "text/csv", "inline_data": "x\n123\n"},
        format="json",
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )
    assert oversized.status_code == 400


def test_asset_dataset_must_be_ready_verified_and_in_tenant(api_client, analytics_org):
    organization, owner, _viewer, _analyst = analytics_org
    api_client.force_authenticate(owner)
    unverified = make_asset(organization, owner, "unverified", checksum_sha256="")
    response = api_client.post(
        "/api/v1/analysis/datasets/",
        {"name": "Asset", "mime_type": "text/csv", "asset": str(unverified.id)},
        format="json",
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )
    assert response.status_code == 400


def test_asset_reader_uses_signed_egress_and_verifies_integrity(analytics_org, monkeypatch):
    organization, owner, _viewer, _analyst = analytics_org
    content = b"x,y\na,1\n"
    asset = make_asset(
        organization,
        owner,
        "verified-input",
        bytes=len(content),
        checksum_sha256=hashlib.sha256(content).hexdigest(),
        metadata={"content_type": "text/csv"},
    )
    monkeypatch.setattr(settings, "IMAGEKIT_ENDPOINT_URL", "https://ik.example.test")
    monkeypatch.setattr(
        "apps.assets.imagekit.generate_signed_delivery_url",
        lambda _path: "https://ik.example.test/signed.csv?token=short-lived",
    )
    seen = {}

    def request(method, url, **kwargs):
        seen.update({"method": method, "url": url, **kwargs})
        return type("Response", (), {"status_code": 200, "content": content})()

    monkeypatch.setattr("apps.tools.egress.safe_request", request)
    assert bytes_for_asset(asset) == content
    assert seen["allowed_hosts"] == ["ik.example.test"]
    assert seen["max_bytes"] == settings.ANALYTICS_MAX_DATASET_BYTES

    asset.checksum_sha256 = "0" * 64
    with pytest.raises(AnalysisError, match="integrity"):
        bytes_for_asset(asset)


def test_grants_require_owner_and_organization_member(api_client, analytics_org, django_user_model):
    organization, owner, viewer, analyst = analytics_org
    dataset = make_dataset(organization, owner)
    api_client.force_authenticate(viewer)
    denied = api_client.post(
        "/api/v1/analysis/dataset-grants/",
        {"dataset": str(dataset.id), "user": str(analyst.id), "permission": "analyze"},
        format="json",
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )
    assert denied.status_code == 403

    outsider = django_user_model.objects.create_user(
        username="grant-outsider", supabase_user_id="grant-outsider"
    )
    api_client.force_authenticate(owner)
    invalid = api_client.post(
        "/api/v1/analysis/dataset-grants/",
        {"dataset": str(dataset.id), "user": str(outsider.id), "permission": "analyze"},
        format="json",
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )
    assert invalid.status_code == 400

    created = api_client.post(
        "/api/v1/analysis/dataset-grants/",
        {"dataset": str(dataset.id), "user": str(analyst.id), "permission": "analyze"},
        format="json",
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )
    assert created.status_code == 201


def test_deleting_dataset_soft_deletes_generated_artifacts_not_the_input_asset(api_client, analytics_org):
    organization, owner, _viewer, _analyst = analytics_org
    source = make_asset(organization, owner, "source-input")
    dataset = make_dataset(organization, owner, inline_data="", asset=source)
    result_asset = make_asset(organization, owner, "generated-result")
    run = AnalysisRun.objects.create(
        dataset=dataset,
        owner=owner,
        status=AnalysisRun.Status.COMPLETED,
        result_asset=result_asset,
    )
    chart_asset = make_asset(organization, owner, "generated-chart", format="png")
    Visualization.objects.create(
        analysis_run=run,
        kind=Visualization.Kind.HISTOGRAM,
        x_column="revenue",
        artifact_asset=chart_asset,
    )
    api_client.force_authenticate(owner)
    response = api_client.delete(
        f"/api/v1/analysis/datasets/{dataset.id}/",
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )
    assert response.status_code == 204
    source.refresh_from_db()
    result_asset.refresh_from_db()
    chart_asset.refresh_from_db()
    assert source.status == Asset.Status.READY
    assert result_asset.status == Asset.Status.DELETED
    assert chart_asset.status == Asset.Status.DELETED


def test_view_grant_cannot_submit_analysis_but_analyze_grant_can(api_client, analytics_org, monkeypatch):
    organization, owner, viewer, analyst = analytics_org
    dataset = make_dataset(organization, owner)
    DatasetGrant.objects.create(dataset=dataset, user=viewer, permission=DatasetGrant.Permission.VIEW)
    DatasetGrant.objects.create(dataset=dataset, user=analyst, permission=DatasetGrant.Permission.ANALYZE)
    monkeypatch.setattr(execute_analysis_run, "delay", lambda *_args, **_kwargs: None)

    api_client.force_authenticate(viewer)
    denied = api_client.post(
        "/api/v1/analysis/runs/",
        {"dataset": str(dataset.id), "transform": {"limit": 1}},
        format="json",
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )
    assert denied.status_code == 404

    api_client.force_authenticate(analyst)
    accepted = api_client.post(
        "/api/v1/analysis/runs/",
        {"dataset": str(dataset.id), "transform": {"limit": 1}},
        format="json",
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )
    assert accepted.status_code == 202


def test_transform_language_is_bounded_and_deterministic():
    frame = dataframe_from_bytes(b"region,revenue\nEast,20\nWest,10\nEast,30\n", mime_type="text/csv")
    result = apply_transform(
        frame,
        {
            "filters": [{"column": "revenue", "operator": "gte", "value": 20}],
            "group_by": ["region"],
            "aggregate": {"revenue": "sum"},
            "sort": [{"column": "revenue_sum", "direction": "desc"}],
        },
    )
    assert result.to_dict(orient="records") == [{"region": "East", "revenue_sum": 50}]
    with pytest.raises(AnalysisError, match="Unsupported transform"):
        apply_transform(frame, {"python": "__import__('os')"})
    literal = apply_transform(
        frame,
        {"filters": [{"column": "region", "operator": "contains", "value": "["}]},
    )
    assert literal.empty  # "contains" is literal and cannot trigger regex parsing.


def test_analysis_task_persists_exact_result_asset(analytics_org, monkeypatch):
    organization, owner, _viewer, _analyst = analytics_org
    dataset = make_dataset(organization, owner)
    run = AnalysisRun.objects.create(
        dataset=dataset,
        owner=owner,
        transform={"filters": [{"column": "region", "operator": "eq", "value": "East"}]},
    )
    captured = {}

    def register(content, **kwargs):
        captured["content"] = content
        return make_asset(
            organization,
            owner,
            "analysis-result",
            bytes=len(content),
            checksum_sha256="b" * 64,
            original_filename=kwargs["file_name"],
        )

    monkeypatch.setattr("apps.assets.services.register_generated_asset", register)
    execute_analysis_run(str(run.id))
    run.refresh_from_db()
    assert run.status == AnalysisRun.Status.COMPLETED
    assert run.result_asset_id is not None
    assert run.result_schema["rows"] == 2
    assert b"West" not in captured["content"]
    assert len(run.result_preview) == 2


def test_visualization_uses_persisted_result_and_json_safe_plotly(analytics_org, monkeypatch):
    organization, owner, _viewer, _analyst = analytics_org
    dataset = make_dataset(organization, owner)
    result_asset = make_asset(organization, owner, "persisted-result")
    run = AnalysisRun.objects.create(
        dataset=dataset,
        owner=owner,
        status=AnalysisRun.Status.COMPLETED,
        result_asset=result_asset,
    )
    visualization = Visualization.objects.create(
        analysis_run=run,
        kind=Visualization.Kind.BAR,
        x_column="region",
        y_column="revenue",
    )
    monkeypatch.setattr(
        "apps.analytics.services.bytes_for_asset",
        lambda _asset: b"region,revenue\nEast,50\n",
    )

    def register(content, **kwargs):
        return make_asset(
            organization,
            owner,
            "chart-artifact",
            format="png",
            original_filename=kwargs["file_name"],
            bytes=len(content),
            checksum_sha256="c" * 64,
        )

    monkeypatch.setattr("apps.assets.services.register_generated_asset", register)
    execute_visualization(str(visualization.id))
    visualization.refresh_from_db()
    assert visualization.status == Visualization.Status.READY
    assert visualization.artifact_asset_id is not None
    assert visualization.result_schema["points"] == 1
    json.dumps(visualization.plotly_spec)


def test_workers_are_idempotent_and_recovery_closes_stalled_records(analytics_org, monkeypatch):
    organization, owner, _viewer, _analyst = analytics_org
    dataset = make_dataset(organization, owner)
    completed = AnalysisRun.objects.create(dataset=dataset, owner=owner, status=AnalysisRun.Status.COMPLETED)
    monkeypatch.setattr(
        "apps.assets.services.register_generated_asset",
        lambda *_args, **_kwargs: pytest.fail("completed jobs must not execute twice"),
    )
    execute_analysis_run(str(completed.id))

    stalled = AnalysisRun.objects.create(
        dataset=dataset,
        owner=owner,
        status=AnalysisRun.Status.RUNNING,
        started_at=timezone.now() - timedelta(minutes=settings.ANALYTICS_STALLED_AFTER_MINUTES + 1),
    )
    orphan = make_asset(
        organization,
        owner,
        "orphan-result",
        provenance={"kind": "analytics-result", "analysis_run_id": "lost"},
    )
    Asset.objects.filter(id=orphan.id).update(
        created_at=timezone.now() - timedelta(hours=settings.ASSET_ORPHAN_GRACE_HOURS + 1)
    )
    result = recover_stalled_analytics()
    stalled.refresh_from_db()
    orphan.refresh_from_db()
    assert result["analysis_runs"] == 1
    assert result["orphan_assets"] == 1
    assert stalled.status == AnalysisRun.Status.FAILED
    assert stalled.completed_at is not None
    assert orphan.status == Asset.Status.DELETED


def test_analytics_tasks_have_dedicated_routes_and_limits():
    assert (
        settings.CELERY_TASK_ROUTES["apps.analytics.tasks.execute_analysis_run"]["queue"]
        == "analytics.analysis"
    )
    assert (
        settings.CELERY_TASK_ROUTES["apps.analytics.tasks.execute_visualization"]["queue"]
        == "analytics.visualization"
    )
    assert execute_analysis_run.soft_time_limit == settings.ANALYTICS_TASK_SOFT_TIME_LIMIT_SECONDS
    assert execute_analysis_run.time_limit == settings.ANALYTICS_TASK_TIME_LIMIT_SECONDS


def test_streamlit_service_is_read_only_and_database_free():
    source = (Path(settings.BASE_DIR) / "streamlit_app" / "app.py").read_text()
    lowered = source.lower()
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "django" not in imports
    assert "database_url" not in lowered
    assert ".post(" not in lowered
    assert ".put(" not in lowered
    assert ".patch(" not in lowered
    assert ".delete(" not in lowered
    assert "text_input" not in lowered
    assert 'client.get(f"{api_base}/visualizations/"' in lowered
