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
        "storage_object_id": f"jt-code/{label}.csv",
        "storage_key": f"jt-code/{label}.csv",
        "storage_bucket": "jt-code-assets",
        "storage_url": "",
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
    monkeypatch.setattr(settings, "SUPABASE_STORAGE_API_URL", "https://project.supabase.co/storage/v1")
    monkeypatch.setattr(
        "apps.assets.supabase_storage.generate_signed_delivery_url",
        lambda _path: "https://project.supabase.co/storage/v1/object/sign/signed.csv?token=short-lived",
    )
    seen = {}

    def request(method, url, **kwargs):
        seen.update({"method": method, "url": url, **kwargs})
        return type("Response", (), {"status_code": 200, "content": content})()

    monkeypatch.setattr("apps.tools.egress.safe_request", request)
    assert bytes_for_asset(asset) == content
    assert seen["allowed_hosts"] == ["project.supabase.co"]
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
            kwargs["file_name"],
            format="png",
            original_filename=kwargs["file_name"],
            bytes=len(content),
            checksum_sha256=hashlib.sha256(content).hexdigest(),
        )

    monkeypatch.setattr("apps.assets.services.register_generated_asset", register)
    execute_visualization(str(visualization.id))
    visualization.refresh_from_db()
    assert visualization.status == Visualization.Status.READY
    assert visualization.artifact_asset_id is not None
    assert visualization.result_schema["points"] == 1
    assert visualization.spec_asset_id is not None
    assert visualization.spec_asset.original_filename.endswith(".plotly.json")
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
    tree = ast.parse(source)
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "django" not in imports and "psycopg" not in imports
    lowered = source.lower()
    assert "database_url" not in lowered and "secret" not in lowered
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"get", "post", "put", "patch", "delete"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "client"
    ]
    # Every JT-Code API call is a GET; the single POST is the Supabase sign-in.
    assert calls.count("post") == 1 and not {"put", "patch", "delete"} & set(calls)
    sign_in = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "supabase_sign_in"
    )
    assert "/auth/v1/token?grant_type=password" in ast.get_source_segment(source, sign_in)
    assert "client.post(" not in "".join(
        ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name != "supabase_sign_in"
    )


# --- Isolation, ACL and new endpoints -------------------------------------------


def test_sandbox_child_gets_no_credentials_and_isolated_interpreter(monkeypatch):
    import subprocess

    from apps.analytics import sandbox

    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(argv=argv, env=kwargs["env"], cwd=kwargs["cwd"])
        return subprocess.CompletedProcess(argv, 0, stdout=b'{"ok": true, "profile": {}}', stderr=b"")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run_engine("profile", b"x\n1\n", mime_type="text/csv")
    assert captured["argv"][1] == "-I" and captured["argv"][2].endswith("engine.py")
    joined = " ".join(f"{key}={value}" for key, value in captured["env"].items())
    for secret in (
        "DATABASE_URL",
        "DJANGO_SECRET_KEY",
        "SUPABASE_SECRET_KEY",
        "SUPABASE",
        "GEMINI",
        "OPENAI",
    ):
        assert secret not in joined
    assert captured["cwd"] == captured["env"]["HOME"]


def test_sandbox_enforces_memory_and_time_limits(settings, monkeypatch):
    import subprocess

    from apps.analytics import sandbox

    settings.ANALYTICS_SANDBOX_MEMORY_MB = 48
    with pytest.raises((AnalysisError, sandbox.SandboxError)):
        sandbox.run_engine("profile", b"x\n1\n", mime_type="text/csv")

    def too_slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(sandbox.subprocess, "run", too_slow)
    with pytest.raises(AnalysisError, match="time limit"):
        sandbox.run_engine("profile", b"x\n1\n", mime_type="text/csv")


def test_inline_dataset_is_profiled_in_the_sandbox(api_client, analytics_org):
    organization, owner, _viewer, _analyst = analytics_org
    api_client.force_authenticate(owner)
    response = api_client.post(
        "/api/v1/analysis/datasets/",
        {"name": "Sales", "mime_type": "text/csv", "inline_data": "team,amount\na,5\nb,7\n"},
        format="json",
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )
    assert response.status_code == 201, response.content
    profiles = {column["name"]: column for column in response.json()["profile"]["columnProfiles"]}
    assert profiles["amount"]["kind"] == "numeric" and profiles["amount"]["mean"] == 6.0
    assert profiles["team"]["topValues"][0]["count"] == 1


def test_datasets_cannot_wrap_another_members_private_asset(api_client, analytics_org):
    organization, owner, viewer, _analyst = analytics_org
    private = make_asset(organization, owner, "private-input", metadata={"content_type": "text/csv"})
    editor_role = Role.objects.get(name=Role.RoleType.EDITOR)
    UserRole.objects.create(user=viewer, role=editor_role, organization=organization)
    api_client.force_authenticate(viewer)
    response = api_client.post(
        "/api/v1/analysis/datasets/",
        {"name": "Leak", "mime_type": "text/csv", "asset": str(private.id)},
        format="json",
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )
    assert response.status_code == 400
    assert "not found" in str(response.json()).lower()


def test_end_to_end_pie_chart_stores_png_and_spec_with_valid_schema(analytics_org, monkeypatch):
    organization, owner, _viewer, _analyst = analytics_org
    dataset = make_dataset(organization, owner)
    run = AnalysisRun.objects.create(
        dataset=dataset,
        owner=owner,
        transform={"group_by": ["region"], "aggregate": {"revenue": "sum"}},
    )
    stored: dict[str, bytes] = {}

    def register(content, **kwargs):
        stored[kwargs["file_name"]] = content
        return make_asset(
            organization,
            owner,
            kwargs["file_name"],
            bytes=len(content),
            checksum_sha256=hashlib.sha256(content).hexdigest(),
            original_filename=kwargs["file_name"],
            metadata={"content_type": kwargs["content_type"]},
        )

    monkeypatch.setattr("apps.assets.services.register_generated_asset", register)
    execute_analysis_run(str(run.id))
    run.refresh_from_db()
    assert run.status == AnalysisRun.Status.COMPLETED, run.error_message
    assert run.result_schema["checksumSha256"] == hashlib.sha256(stored[f"analysis-{run.id}.csv"]).hexdigest()
    monkeypatch.setattr(
        "apps.analytics.services.bytes_for_asset", lambda asset: stored[asset.original_filename]
    )
    chart = Visualization.objects.create(
        analysis_run=run,
        kind=Visualization.Kind.PIE,
        x_column="region",
        y_column="revenue_sum",
        title="Revenue",
    )
    execute_visualization(str(chart.id))
    chart.refresh_from_db()
    assert chart.status == Visualization.Status.READY, chart.error_message
    assert chart.result_schema["kind"] == "pie" and chart.result_schema["points"] == 2
    assert stored[f"visualization-{chart.id}.png"].startswith(b"\x89PNG")
    assert json.loads(stored[f"visualization-{chart.id}.plotly.json"])["data"][0]["type"] == "pie"


def test_run_retry_download_and_delete(
    api_client, analytics_org, monkeypatch, django_capture_on_commit_callbacks
):
    organization, owner, viewer, _analyst = analytics_org
    dataset = make_dataset(organization, owner)
    result = make_asset(organization, owner, "run-result")
    completed = AnalysisRun.objects.create(
        dataset=dataset, owner=owner, status=AnalysisRun.Status.COMPLETED, result_asset=result
    )
    failed = AnalysisRun.objects.create(
        dataset=dataset, owner=owner, status=AnalysisRun.Status.FAILED, transform={"limit": 1}
    )
    queued: list[str] = []
    monkeypatch.setattr(execute_analysis_run, "delay", queued.append)
    monkeypatch.setattr("apps.assets.supabase_storage.stream_file", lambda path: iter([b"region\nEast\n"]))
    api_client.force_authenticate(owner)
    headers = {"HTTP_X_ORGANIZATION_ID": str(organization.id)}

    with django_capture_on_commit_callbacks(execute=True):
        retried = api_client.post(f"/api/v1/analysis/runs/{failed.id}/retry/", **headers)
    assert retried.status_code == 202 and queued == [retried.json()["id"]]
    assert api_client.post(f"/api/v1/analysis/runs/{completed.id}/retry/", **headers).status_code == 409

    download = api_client.get(f"/api/v1/analysis/runs/{completed.id}/download/", **headers)
    assert download.status_code == 200 and b"".join(download.streaming_content) == b"region\nEast\n"

    api_client.force_authenticate(viewer)
    assert api_client.delete(f"/api/v1/analysis/runs/{completed.id}/", **headers).status_code == 404
    api_client.force_authenticate(owner)
    assert api_client.delete(f"/api/v1/analysis/runs/{completed.id}/", **headers).status_code == 204
    result.refresh_from_db()
    assert result.status == Asset.Status.DELETED


def test_visualization_columns_must_exist_in_the_result(api_client, analytics_org):
    organization, owner, _viewer, _analyst = analytics_org
    run = AnalysisRun.objects.create(
        dataset=make_dataset(organization, owner),
        owner=owner,
        status=AnalysisRun.Status.COMPLETED,
        result_schema={"columns": ["region", "revenue"]},
    )
    api_client.force_authenticate(owner)
    response = api_client.post(
        "/api/v1/visualizations/",
        {"analysis_run": str(run.id), "kind": "bar", "x_column": "region", "y_column": "missing"},
        format="json",
        HTTP_X_ORGANIZATION_ID=str(organization.id),
    )
    assert response.status_code == 400 and "y_column" in response.json().get("details", response.json())
