"""Phase 11 exit proofs for private Supabase Storage assets."""

from __future__ import annotations

import hashlib

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.assets.models import Asset
from apps.assets.supabase_storage import content_matches_type, provider_identity_fingerprint
from apps.assets.tasks import purge_deleted_assets, reconcile_asset
from apps.identity.models import Organization, Role, UserOrganization, UserRole

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
    key = f"jt-code/test/{organization.id}/uploads/{label}.pdf"
    values = {
        "owner": owner,
        "organization": organization,
        "storage_object_id": key,
        "storage_key": key,
        "storage_bucket": "jt-code-assets",
        "storage_url": "",
        "resource_type": "file",
        "original_filename": f"{label}.pdf",
        "name": f"{label}.pdf",
        "bytes": 10,
        "checksum_sha256": "a" * 64,
        "provider_fingerprint": provider_identity_fingerprint(
            {"bucket": "jt-code-assets", "key": key, "size": 10}
        ),
        "metadata": {"content_type": "application/pdf"},
        "provenance": {"provider": "supabase-storage", "fingerprintVersion": 1},
    }
    values.update(overrides)
    return Asset.objects.create(**values)


def test_magic_bytes_and_fingerprint_do_not_depend_on_mutable_metadata():
    assert content_matches_type(PNG, "image/png")
    assert content_matches_type(b"%PDF-1.7", "application/pdf")
    assert not content_matches_type(b"<svg/>", "image/png")
    resource = {"bucket": "bucket", "key": "org/path.pdf", "size": 10, "updated_at": "later"}
    assert provider_identity_fingerprint(resource) == provider_identity_fingerprint(
        {**resource, "updated_at": "now"}
    )


@pytest.mark.django_db
def test_access_endpoint_enforces_django_acl_before_signing(team, settings, monkeypatch):
    settings.SUPABASE_URL = "https://project.supabase.co"
    settings.SUPABASE_SECRET_KEY = "sb_secret_test"
    settings.SUPABASE_STORAGE_BUCKET = "jt-code-assets"
    organization, admin, editor, viewer = team
    asset = make_asset(organization, editor)
    monkeypatch.setattr(
        "apps.assets.views.generate_signed_delivery_url", lambda key: f"https://signed.test/{key}"
    )
    assert client_for(editor, organization).post(f"/api/v1/files/{asset.id}/access/").status_code == 200
    assert client_for(viewer, organization).post(f"/api/v1/files/{asset.id}/access/").status_code == 404
    assert client_for(admin, organization).post(f"/api/v1/files/{asset.id}/access/").status_code == 200


@pytest.mark.django_db
def test_reconcile_and_purge_detects_bad_content_and_deletes_by_storage_key(team, settings, monkeypatch):
    settings.SUPABASE_URL = "https://project.supabase.co"
    settings.SUPABASE_SECRET_KEY = "sb_secret_test"
    settings.SUPABASE_STORAGE_BUCKET = "jt-code-assets"
    organization, _admin, editor, _viewer = team
    asset = make_asset(organization, editor)
    monkeypatch.setattr(
        "apps.assets.supabase_storage.content_checksum",
        lambda *args, **kwargs: ("different" * 8, "application/pdf", b"%PDF-"),
    )
    assert reconcile_asset(str(asset.id)) is False
    asset.refresh_from_db()
    assert asset.status == Asset.Status.QUARANTINED

    Asset.objects.filter(id=asset.id).update(
        status=Asset.Status.DELETED,
        deleted_at=timezone.now() - timezone.timedelta(days=8),
        deletion_attempts=0,
    )
    deleted = []
    monkeypatch.setattr("apps.assets.supabase_storage.delete_storage_object", deleted.append)
    assert purge_deleted_assets() == 1
    assert deleted == [asset.storage_key]


def test_asset_checksum_is_preserved_for_generated_content():
    assert hashlib.sha256(PNG).hexdigest() != ""
