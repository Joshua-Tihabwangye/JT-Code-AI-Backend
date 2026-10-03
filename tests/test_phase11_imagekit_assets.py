"""Phase 11 exit proofs: signed delivery, provenance, tenant deletion lifecycle."""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import parse_qs, urlsplit

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.assets.imagekit import ImageKitNotFound, generate_signed_delivery_url
from apps.assets.models import Asset
from apps.assets.services import register_generated_asset
from apps.assets.tasks import purge_deleted_assets, reconcile_asset, reconcile_assets
from apps.identity.models import Organization


@pytest.fixture
def asset_org(db, user):
    organization = Organization.objects.create(name="Asset Org", slug="asset-phase11", owner=user)
    user.organizations.add(organization)
    return organization


@pytest.fixture
def asset(user, asset_org):
    return Asset.objects.create(
        owner=user,
        organization=asset_org,
        imagekit_file_id="asset-phase11-file",
        imagekit_file_path=f"/jt-code/{user.id}/report.pdf",
        secure_url="https://ik.imagekit.io/jt-code/report.pdf",
        resource_type="non-image",
        original_filename="report.pdf",
        checksum_sha256="a" * 64,
        provenance={"provider": "imagekit"},
    )


@pytest.mark.django_db
def test_delivery_endpoint_returns_server_signed_short_lived_url(
    authenticated_client, asset, settings, monkeypatch
):
    settings.IMAGEKIT_PUBLIC_KEY = "public"
    settings.IMAGEKIT_PRIVATE_KEY = "private"
    settings.IMAGEKIT_ENDPOINT_URL = "https://ik.imagekit.io/jt-code"
    settings.IMAGEKIT_SIGNED_URL_TTL_SECONDS = 300
    monkeypatch.setattr("apps.assets.imagekit.current_timestamp", lambda: 1_700_000_000)

    response = authenticated_client.post(f"/api/v1/files/{asset.id}/access/")

    assert response.status_code == 200
    query = parse_qs(urlsplit(response.json()["url"]).query)
    timestamp = query["ik-t"][0]
    expected = hmac.new(
        b"private", f"jt-code/{asset.owner_id}/report.pdf{timestamp}".encode(), hashlib.sha1
    ).hexdigest()
    assert query["ik-s"] == [expected]


@pytest.mark.django_db
def test_soft_delete_hides_asset_and_lifecycle_purges_provider(authenticated_client, asset, monkeypatch):
    response = authenticated_client.delete(f"/api/v1/files/{asset.id}/")
    assert response.status_code == 204
    assert authenticated_client.get("/api/v1/files/").json()["count"] == 0

    asset.refresh_from_db()
    asset.deleted_at = timezone.now() - timezone.timedelta(days=8)
    asset.save(update_fields=["deleted_at"])
    monkeypatch.setattr("apps.assets.imagekit.imagekit_is_configured", lambda: True)
    deleted: list[str] = []
    monkeypatch.setattr("apps.assets.imagekit.delete_imagekit_file", lambda file_id: deleted.append(file_id))

    assert purge_deleted_assets() == 1
    asset.refresh_from_db()
    assert deleted == [asset.imagekit_file_id]
    assert asset.provider_deleted_at is not None


def test_signed_delivery_helper_uses_imagekit_documented_relative_path(settings, monkeypatch):
    settings.IMAGEKIT_PRIVATE_KEY = "private"
    settings.IMAGEKIT_ENDPOINT_URL = "https://ik.imagekit.io/jt-code"
    settings.IMAGEKIT_SIGNED_URL_TTL_SECONDS = 60
    monkeypatch.setattr("apps.assets.imagekit.current_timestamp", lambda: 100)
    assert generate_signed_delivery_url("/folder/file.png") == (
        "https://ik.imagekit.io/jt-code/folder/file.png?ik-t=160&ik-s="
        "1d4a6e73cc475e81d83c197048bef07b7fa69a5f"
    )


@pytest.mark.django_db
def test_viewer_cannot_delete_an_organization_asset(asset, asset_org, django_user_model):
    viewer = django_user_model.objects.create_user(
        username="asset-viewer",
        supabase_user_id="asset-viewer",
        email="asset-viewer@example.test",
    )
    viewer.organizations.add(asset_org)
    client = APIClient()
    client.force_authenticate(viewer)

    response = client.delete(f"/api/v1/files/{asset.id}/")

    assert response.status_code == 403
    asset.refresh_from_db()
    assert asset.status == Asset.Status.READY


@pytest.mark.django_db
def test_reconciliation_distinguishes_missing_objects_from_transient_errors(asset, monkeypatch):
    monkeypatch.setattr(
        "apps.assets.imagekit.verify_imagekit_file",
        lambda file_id: (_ for _ in ()).throw(RuntimeError("temporary outage")),
    )
    assert reconcile_asset(str(asset.id)) is False
    asset.refresh_from_db()
    assert asset.status == Asset.Status.READY
    assert "Transient" in asset.deletion_error

    monkeypatch.setattr(
        "apps.assets.imagekit.verify_imagekit_file",
        lambda file_id: (_ for _ in ()).throw(ImageKitNotFound("gone")),
    )
    assert reconcile_asset(str(asset.id)) is False
    asset.refresh_from_db()
    assert asset.status == Asset.Status.QUARANTINED


@pytest.mark.django_db
def test_reconciliation_deletes_only_aged_unregistered_provider_files(asset, settings, monkeypatch):
    settings.IMAGEKIT_UPLOAD_FOLDER = "jt-code/test"
    settings.ASSET_ORPHAN_GRACE_HOURS = 24
    settings.IMAGEKIT_RECONCILE_PAGE_SIZE = 100
    settings.IMAGEKIT_RECONCILE_MAX_PAGES = 2
    monkeypatch.setattr("apps.assets.imagekit.imagekit_is_configured", lambda: True)
    monkeypatch.setattr("apps.assets.tasks.reconcile_asset", lambda asset_id: True)
    old = (timezone.now() - timezone.timedelta(days=2)).isoformat()
    recent = timezone.now().isoformat()
    monkeypatch.setattr(
        "apps.assets.imagekit.list_imagekit_files",
        lambda **kwargs: [
            {"fileId": asset.imagekit_file_id, "createdAt": old},
            {"fileId": "aged-orphan", "createdAt": old},
            {"fileId": "recent-orphan", "createdAt": recent},
        ],
    )
    deleted = []
    monkeypatch.setattr("apps.assets.imagekit.delete_imagekit_file", deleted.append)

    result = reconcile_assets()

    assert result == {"verified": 1, "quarantined": 0, "orphans_deleted": 1}
    assert deleted == ["aged-orphan"]


@pytest.mark.django_db
def test_server_generated_bytes_are_registered_with_checksum_and_provenance(user, asset_org, monkeypatch):
    content = b"generated chart bytes"
    monkeypatch.setattr(
        "apps.assets.services.upload_bytes_to_imagekit_details",
        lambda *args, **kwargs: {
            "fileId": "generated-file",
            "filePath": f"/jt-code/{user.id}/chart.png",
            "url": "https://ik.imagekit.io/jt-code/chart.png",
            "size": len(content),
            "fileType": "image",
            "format": "png",
            "updatedAt": "2026-10-01T00:00:00Z",
        },
    )

    generated = register_generated_asset(
        content,
        owner=user,
        organization=asset_org,
        file_name="chart.png",
        folder=f"/jt-code/{user.id}",
        content_type="image/png",
        provenance={"visualization_id": "example"},
    )

    assert generated.checksum_sha256 == hashlib.sha256(content).hexdigest()
    assert generated.provider_fingerprint
    assert generated.provenance["visualization_id"] == "example"
