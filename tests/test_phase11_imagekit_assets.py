"""Phase 11 exit proofs: private provider storage, signed delivery, ownership,
reference-aware lifecycle, reconciliation and the frontend files contract."""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from apps.assets.imagekit import (
    ImageKitNotFound,
    content_matches_type,
    generate_signed_delivery_url,
    provider_identity_fingerprint,
)
from apps.assets.models import Asset, ConversationAttachment
from apps.assets.services import register_generated_asset
from apps.assets.tasks import purge_deleted_assets, reconcile_asset, reconcile_assets, sweep_orphans
from apps.identity.models import Organization, Role, UserOrganization, UserRole

pytestmark = pytest.mark.django_db
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def member(django_user_model, organization, label, role):
    person = django_user_model.objects.create_user(
        username=label, supabase_user_id=f"supabase-{label}", email=f"{label}@example.test"
    )
    UserOrganization.objects.create(user=person, organization=organization)
    role_record, _ = Role.objects.get_or_create(name=role)
    UserRole.objects.get_or_create(user=person, role=role_record, organization=organization)
    return person


@pytest.fixture
def imagekit(settings):
    settings.IMAGEKIT_PUBLIC_KEY = "public"
    settings.IMAGEKIT_PRIVATE_KEY = "private"
    settings.IMAGEKIT_ENDPOINT_URL = "https://ik.imagekit.io/jt-code"
    settings.IMAGEKIT_UPLOAD_FOLDER = "jt-code/test"
    return settings


@pytest.fixture
def team(django_user_model):
    organization = Organization.objects.create(name="Asset Org", slug="asset-phase11")
    admin = member(django_user_model, organization, "asset-admin", Role.RoleType.ADMIN)
    editor = member(django_user_model, organization, "asset-editor", Role.RoleType.EDITOR)
    viewer = member(django_user_model, organization, "asset-viewer", Role.RoleType.VIEWER)
    return organization, admin, editor, viewer


def client_for(person, organization):
    client = APIClient()
    client.force_authenticate(person)
    client.credentials(HTTP_X_ORGANIZATION_ID=str(organization.id))
    return client


def make_asset(organization, owner, label="report", **overrides):
    values = {
        "owner": owner,
        "organization": organization,
        "imagekit_file_id": f"file-{label}",
        "imagekit_file_path": f"/jt-code/test/{organization.id}/uploads/{label}.pdf",
        "secure_url": f"https://ik.imagekit.io/jt-code/{label}.pdf",
        "resource_type": "non-image",
        "original_filename": f"{label}.pdf",
        "name": f"{label}.pdf",
        "bytes": 10,
        "checksum_sha256": "a" * 64,
        "metadata": {"content_type": "application/pdf"},
        "provenance": {"provider": "imagekit", "fingerprintVersion": 2},
    }
    values.update(overrides)
    return Asset.objects.create(**values)


def fake_upload(content_holder):
    def upload(content, *, file_name, folder, content_type):
        content_holder.append({"file_name": file_name, "folder": folder, "content_type": content_type})
        return {
            "fileId": f"gen-{len(content_holder)}",
            "filePath": f"{folder}/{file_name}",
            "url": f"https://ik.imagekit.io/jt-code{folder}/{file_name}",
            "size": len(content),
            "fileType": "image",
            "format": "png",
            "versionInfo": {"id": "v1", "name": "Version 1"},
        }

    return upload


# --- Delivery & signing -------------------------------------------------------


def test_signed_delivery_helper_uses_imagekit_documented_relative_path(settings, monkeypatch):
    settings.IMAGEKIT_PRIVATE_KEY = "private"
    settings.IMAGEKIT_ENDPOINT_URL = "https://ik.imagekit.io/jt-code"
    settings.IMAGEKIT_SIGNED_URL_TTL_SECONDS = 60
    monkeypatch.setattr("apps.assets.imagekit.current_timestamp", lambda: 100)
    assert generate_signed_delivery_url("/folder/file.png") == (
        "https://ik.imagekit.io/jt-code/folder/file.png?ik-t=160&ik-s=1d4a6e73cc475e81d83c197048bef07b7fa69a5f"
    )


def test_access_endpoint_signs_only_for_authorized_readers(team, imagekit, monkeypatch):
    organization, admin, editor, viewer = team
    monkeypatch.setattr("apps.assets.imagekit.current_timestamp", lambda: 1_700_000_000)
    asset = make_asset(organization, editor)
    response = client_for(editor, organization).post(f"/api/v1/files/{asset.id}/access/")
    assert response.status_code == 200
    query = parse_qs(urlsplit(response.json()["url"]).query)
    relative = asset.imagekit_file_path.lstrip("/")
    expected = hmac.new(b"private", f"{relative}{query['ik-t'][0]}".encode(), hashlib.sha1).hexdigest()
    assert query["ik-s"] == [expected]
    assert client_for(viewer, organization).post(f"/api/v1/files/{asset.id}/access/").status_code == 404
    assert client_for(admin, organization).post(f"/api/v1/files/{asset.id}/access/").status_code == 200
    asset.visibility = Asset.Visibility.ORGANIZATION
    asset.save(update_fields=["visibility"])
    assert client_for(viewer, organization).post(f"/api/v1/files/{asset.id}/access/").status_code == 200


# --- Server-generated assets ---------------------------------------------------


def test_generated_assets_are_private_unique_tenant_scoped_and_reconcile_cleanly(team, imagekit, monkeypatch):
    organization, _admin, editor, _viewer = team
    uploads: list[dict] = []
    monkeypatch.setattr("apps.assets.services.upload_bytes_to_imagekit_details", fake_upload(uploads))
    first = register_generated_asset(
        b"chart",
        owner=editor,
        organization=organization,
        file_name="chart.png",
        kind="analytics/charts",
        content_type="image/png",
        provenance={"kind": "analytics-visualization"},
    )
    second = register_generated_asset(
        b"chart",
        owner=editor,
        organization=organization,
        file_name="chart.png",
        kind="analytics/charts",
        content_type="image/png",
    )
    assert uploads[0]["folder"] == f"/jt-code/test/{organization.id}/analytics/charts"
    assert uploads[0]["file_name"] != uploads[1]["file_name"], "re-generation must never overwrite"
    assert first.checksum_sha256 == hashlib.sha256(b"chart").hexdigest()
    assert first.provenance["kind"] == "analytics-visualization"
    assert first.visibility == Asset.Visibility.PRIVATE and second.id != first.id

    # The provider details payload carries mutable metadata (updatedAt, tags);
    # only a version/path/size change may quarantine the record.
    details = {
        "fileId": first.imagekit_file_id,
        "filePath": first.imagekit_file_path,
        "size": 5,
        "versionInfo": {"id": "v1"},
        "type": "file",
        "updatedAt": "2026-10-04T00:00:00Z",
        "tags": ["x"],
    }
    monkeypatch.setattr("apps.assets.imagekit.verify_imagekit_file", lambda file_id: details)
    assert reconcile_asset(str(first.id)) is True
    first.refresh_from_db()
    assert first.status == Asset.Status.READY and first.last_verified_at is not None
    details["versionInfo"] = {"id": "v2"}
    assert reconcile_asset(str(first.id)) is False
    first.refresh_from_db()
    assert first.status == Asset.Status.QUARANTINED


def test_server_upload_sends_private_non_overwriting_request(imagekit, monkeypatch):
    from apps.assets import imagekit as client

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"fileId": "f", "filePath": "/p/x.png", "url": "u", "size": 3, "fileType": "image"}

    def fake_post(url, **kwargs):
        captured.update(url=url, data=kwargs["data"])
        return Response()

    monkeypatch.setattr(client.httpx, "post", fake_post)
    client.upload_bytes_to_imagekit_details(b"abc", file_name="x.png", folder="/p", content_type="image/png")
    assert captured["url"] == "https://upload.imagekit.io/api/v1/files/upload"
    assert captured["data"]["isPrivateFile"] == "true"
    assert captured["data"]["overwriteFile"] == "false"


def test_legacy_fingerprints_are_upgraded_not_quarantined(team, monkeypatch):
    organization, _admin, editor, _viewer = team
    asset = make_asset(
        organization, editor, provider_fingerprint="legacy", provenance={"provider": "imagekit"}
    )
    details = {
        "fileId": asset.imagekit_file_id,
        "filePath": asset.imagekit_file_path,
        "size": 10,
        "versionInfo": {"id": "v9"},
        "type": "file",
    }
    monkeypatch.setattr("apps.assets.imagekit.verify_imagekit_file", lambda file_id: details)
    assert reconcile_asset(str(asset.id)) is True
    asset.refresh_from_db()
    assert asset.provider_fingerprint == provider_identity_fingerprint(details)
    assert asset.provenance["fingerprintVersion"] == 2


def test_reconciliation_distinguishes_missing_objects_from_transient_errors(team, monkeypatch):
    organization, _admin, editor, _viewer = team
    asset = make_asset(organization, editor)

    def outage(file_id):
        raise RuntimeError("temporary outage")

    monkeypatch.setattr("apps.assets.imagekit.verify_imagekit_file", outage)
    assert reconcile_asset(str(asset.id)) is False
    asset.refresh_from_db()
    assert asset.status == Asset.Status.READY and "Transient" in asset.deletion_error

    def missing(file_id):
        raise ImageKitNotFound("gone")

    monkeypatch.setattr("apps.assets.imagekit.verify_imagekit_file", missing)
    assert reconcile_asset(str(asset.id)) is False
    asset.refresh_from_db()
    assert asset.status == Asset.Status.QUARANTINED


def test_reconcile_runs_in_bounded_batches(team, imagekit, monkeypatch):
    organization, _admin, editor, _viewer = team
    imagekit.IMAGEKIT_RECONCILE_BATCH_SIZE = 2
    for index in range(3):
        make_asset(organization, editor, label=f"batch-{index}")
    seen: list[str] = []
    monkeypatch.setattr("apps.assets.tasks.reconcile_asset", lambda asset_id: seen.append(asset_id) or True)
    assert reconcile_assets() == {"verified": 2, "quarantined": 0}
    assert len(seen) == 2


def test_orphan_sweep_walks_nested_folders_and_keeps_registered_or_recent_files(team, imagekit, monkeypatch):
    organization, _admin, editor, _viewer = team
    imagekit.ASSET_ORPHAN_GRACE_HOURS = 24
    asset = make_asset(organization, editor)
    old = (timezone.now() - timezone.timedelta(days=2)).isoformat()
    recent = timezone.now().isoformat()
    tree = {
        "/jt-code/test": [{"type": "folder", "folderPath": "/jt-code/test/org", "name": "org"}],
        "/jt-code/test/org": [
            {"type": "file", "fileId": asset.imagekit_file_id, "createdAt": old},
            {"type": "file", "fileId": "aged-orphan", "createdAt": old},
            {"type": "file", "fileId": "recent-orphan", "createdAt": recent},
        ],
    }
    monkeypatch.setattr(
        "apps.assets.imagekit.list_imagekit_files",
        lambda *, path, skip=0, limit=100, kind="all": tree.get(path, []) if skip == 0 else [],
    )
    deleted: list[str] = []
    monkeypatch.setattr("apps.assets.imagekit.delete_imagekit_file", deleted.append)
    assert sweep_orphans() == {"scanned": 3, "orphans_deleted": 1}
    assert deleted == ["aged-orphan"]


# --- Lifecycle & reference safety ----------------------------------------------


def test_soft_delete_restore_and_purge_with_attempt_cap(team, imagekit, monkeypatch):
    organization, _admin, editor, _viewer = team
    asset = make_asset(organization, editor)
    api = client_for(editor, organization)
    assert api.delete(f"/api/v1/files/{asset.id}/").status_code == 204
    assert api.get("/api/v1/files/").json() == []
    assert api.post(f"/api/v1/files/{asset.id}/restore/").status_code == 200
    assert len(api.get("/api/v1/files/").json()) == 1

    assert api.delete(f"/api/v1/files/{asset.id}/").status_code == 204
    Asset.objects.filter(id=asset.id).update(deleted_at=timezone.now() - timezone.timedelta(days=8))
    imagekit.ASSET_DELETE_MAX_ATTEMPTS = 2

    def failing(file_id):
        raise RuntimeError("provider down")

    monkeypatch.setattr("apps.assets.imagekit.delete_imagekit_file", failing)
    purge_deleted_assets()
    purge_deleted_assets()
    purge_deleted_assets()
    asset.refresh_from_db()
    assert asset.deletion_attempts == 2 and asset.provider_deleted_at is None

    Asset.objects.filter(id=asset.id).update(deletion_attempts=0)
    deleted: list[str] = []
    monkeypatch.setattr("apps.assets.imagekit.delete_imagekit_file", deleted.append)
    assert purge_deleted_assets() == 1
    asset.refresh_from_db()
    assert deleted == [asset.imagekit_file_id] and asset.provider_deleted_at is not None
    assert api.post(f"/api/v1/files/{asset.id}/restore/").status_code == 409


def test_referenced_assets_need_force_to_delete(team, imagekit):
    from apps.knowledge.models import Collection, Source

    organization, _admin, editor, _viewer = team
    asset = make_asset(organization, editor)
    collection = Collection.objects.create(organization=organization, name="KB", embedding_model="echo")
    source = Source.objects.create(
        collection=collection, source_type="file", name="Report", config={"asset_id": str(asset.id)}
    )
    api = client_for(editor, organization)
    detail = api.get(f"/api/v1/files/{asset.id}/").json()
    assert detail["usedIn"] == [f"knowledge-source:{source.id}"]
    blocked = api.delete(f"/api/v1/files/{asset.id}/")
    assert blocked.status_code == 409 and "knowledge-source" in blocked.json()["detail"]
    assert api.delete(f"/api/v1/files/{asset.id}/?force=true").status_code == 204


def test_only_owner_or_admin_can_change_assets_and_owner_removal_keeps_them(team, django_user_model):
    organization, admin, editor, viewer = team
    other_editor = member(django_user_model, organization, "asset-editor-2", Role.RoleType.EDITOR)
    asset = make_asset(organization, editor, visibility=Asset.Visibility.ORGANIZATION)
    assert client_for(viewer, organization).delete(f"/api/v1/files/{asset.id}/").status_code == 403
    assert (
        client_for(other_editor, organization)
        .patch(f"/api/v1/files/{asset.id}/", {"name": "x"}, format="json")
        .status_code
        == 403
    )
    renamed = client_for(admin, organization).patch(
        f"/api/v1/files/{asset.id}/", {"name": "Q3 report.pdf", "visibility": "private"}, format="json"
    )
    assert renamed.status_code == 200 and renamed.json()["name"] == "Q3 report.pdf"
    editor.delete()
    asset.refresh_from_db()
    assert asset.owner_id is None and asset.status == Asset.Status.READY


def test_bulk_delete_reports_per_item_outcomes(team):
    organization, _admin, editor, _viewer = team
    mine = make_asset(organization, editor, label="mine")
    hidden = make_asset(organization, None, label="hidden")
    response = client_for(editor, organization).post(
        "/api/v1/files/bulk-delete/", {"ids": [str(mine.id), str(hidden.id)]}, format="json"
    )
    assert response.status_code == 200
    assert response.json()["deleted"] == [str(mine.id)]
    assert response.json()["skipped"] == [{"id": str(hidden.id), "reason": "not found"}]


def test_attach_to_conversation_requires_access_to_both(team, django_user_model):
    from apps.conversations.models import Conversation

    organization, _admin, editor, _viewer = team
    asset = make_asset(organization, editor)
    conversation = Conversation.objects.create(owner=editor, organization=organization, title="Chat")
    response = client_for(editor, organization).post(
        f"/api/v1/files/{asset.id}/attach/", {"conversationId": str(conversation.id)}, format="json"
    )
    assert response.status_code == 200, response.content
    assert response.json()["usedIn"] == [f"conversation:{conversation.id}"]
    assert ConversationAttachment.objects.filter(asset=asset, conversation=conversation).exists()
    rival = Organization.objects.create(name="Rival", slug="rival-assets")
    foreign = Conversation.objects.create(owner=editor, organization=rival, title="Other")
    assert (
        client_for(editor, organization)
        .post(f"/api/v1/files/{asset.id}/attach/", {"conversationId": str(foreign.id)}, format="json")
        .status_code
        == 404
    )


# --- Upload & download proxy --------------------------------------------------


def test_multipart_upload_verifies_bytes_and_registers_private_asset(team, imagekit, monkeypatch):
    organization, _admin, editor, viewer = team
    uploads: list[dict] = []
    monkeypatch.setattr("apps.assets.services.upload_bytes_to_imagekit_details", fake_upload(uploads))
    api = client_for(editor, organization)
    response = api.post(
        "/api/v1/files/",
        {"file": SimpleUploadedFile("logo.png", PNG, content_type="image/png")},
        format="multipart",
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["name"] == "logo.png" and body["mimeType"] == "image/png" and body["size"] == len(PNG)
    assert body["blobKey"] == "gen-1" and body["usedIn"] == []
    assert uploads[0]["folder"] == f"/jt-code/test/{organization.id}/uploads/{editor.id}"

    spoofed = api.post(
        "/api/v1/files/",
        {"file": SimpleUploadedFile("evil.png", b"<script>alert(1)</script>", content_type="image/png")},
        format="multipart",
    )
    assert spoofed.status_code == 400
    blocked_type = api.post(
        "/api/v1/files/",
        {"file": SimpleUploadedFile("x.svg", b"<svg/>", content_type="image/svg+xml")},
        format="multipart",
    )
    assert blocked_type.status_code == 400
    assert (
        client_for(viewer, organization)
        .post(
            "/api/v1/files/",
            {"file": SimpleUploadedFile("logo.png", PNG, content_type="image/png")},
            format="multipart",
        )
        .status_code
        == 403
    )


def test_download_streams_bytes_with_safe_headers(team, imagekit, monkeypatch):
    organization, _admin, editor, viewer = team
    asset = make_asset(organization, editor)
    monkeypatch.setattr("apps.assets.views.stream_file", lambda path: iter([b"%PDF-", b"1.7"]))
    response = client_for(editor, organization).get(f"/api/v1/files/{asset.id}/download/")
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"%PDF-1.7"
    assert response["Content-Type"] == "application/pdf"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert 'filename="report.pdf"' in response["Content-Disposition"]
    assert client_for(viewer, organization).get(f"/api/v1/files/{asset.id}/download/").status_code == 404


def test_list_is_tenant_scoped_and_supports_pagination(team, django_user_model):
    organization, _admin, editor, _viewer = team
    rival = Organization.objects.create(name="Rival", slug="rival-files")
    rival_admin = member(django_user_model, rival, "rival-files-admin", Role.RoleType.ADMIN)
    make_asset(organization, editor, label="a")
    make_asset(rival, rival_admin, label="b")
    api = client_for(editor, organization)
    assert [item["name"] for item in api.get("/api/v1/files/").json()] == ["a.pdf"]
    page = api.get("/api/v1/files/", {"page": 1}).json()
    assert page["count"] == 1 and page["results"][0]["name"] == "a.pdf"


def test_magic_byte_checks():
    assert content_matches_type(PNG, "image/png")
    assert content_matches_type(b"RIFF\x00\x00\x00\x00WEBPVP8", "image/webp")
    assert content_matches_type(b"%PDF-1.7", "application/pdf")
    assert content_matches_type(b"name,value\n", "text/csv")
    assert not content_matches_type(b"\x00\x01", "text/plain")
    assert not content_matches_type(b"GIF89a", "image/png")
    assert not content_matches_type(b"<svg/>", "image/svg+xml")


# --- Legacy migration ----------------------------------------------------------


def test_legacy_rows_are_mapped_to_verified_private_imagekit_files(team, imagekit, monkeypatch):
    organization, _admin, editor, _viewer = team
    legacy = make_asset(
        organization,
        editor,
        label="legacy",
        imagekit_file_path="",
        status=Asset.Status.QUARANTINED,
        provenance={"provider": "legacy", "migration_required": True},
    )
    details = {
        "fileId": "ik-legacy",
        "filePath": "/jt-code/test/legacy.pdf",
        "url": "https://ik/legacy.pdf",
        "size": 10,
        "fileType": "non-image",
        "isPrivateFile": True,
        "type": "file",
        "versionInfo": {"id": "v1"},
    }
    monkeypatch.setattr(
        "apps.assets.management.commands.migrate_legacy_assets.verify_imagekit_file", lambda file_id: details
    )
    monkeypatch.setattr(
        "apps.assets.management.commands.migrate_legacy_assets.content_checksum",
        lambda path, expected_size: ("c" * 64, "application/pdf", b"%PDF-"),
    )
    call_command("migrate_legacy_assets", "--map", f"{legacy.id}=ik-legacy")
    legacy.refresh_from_db()
    assert legacy.status == Asset.Status.READY
    assert legacy.imagekit_file_path == "/jt-code/test/legacy.pdf"
    assert legacy.checksum_sha256 == "c" * 64
    assert legacy.provenance["migration_required"] is False
