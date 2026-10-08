"""Direct private Supabase Storage upload and registration contracts."""

from __future__ import annotations

import pytest
from django.test import override_settings

from apps.assets.models import Asset
from apps.identity.models import Organization

STORAGE = {
    "SUPABASE_URL": "https://project.supabase.co",
    "SUPABASE_SECRET_KEY": "sb_secret_test",
    "SUPABASE_STORAGE_BUCKET": "jt-code-assets",
    "SUPABASE_STORAGE_PREFIX": "jt-code/test",
}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 34


@pytest.fixture
def org(user):
    organization = Organization.objects.create(name="Storage Org", slug="storage-org", owner=user)
    user.organizations.add(organization)
    return organization


@pytest.mark.django_db
@override_settings(**STORAGE, ASSET_MAX_UPLOAD_BYTES=1024)
def test_signature_issues_one_object_supabase_upload_capability(authenticated_client, user, org, monkeypatch):
    monkeypatch.setattr(
        "apps.assets.views.create_signed_upload",
        lambda key: (
            f"https://project.supabase.co/storage/v1/object/upload/sign/jt-code-assets/{key}",
            "token",
        ),
    )
    response = authenticated_client.post(
        "/api/v1/files/signature/",
        {"originalFilename": "Quarter Report.pdf", "contentType": "application/pdf", "bytes": 512},
    )
    assert response.status_code == 200, response.content
    payload = response.json()
    assert payload["uploadMethod"] == "PUT"
    assert payload["uploadHeaders"]["x-signature"] == "token"
    assert payload["uploadHeaders"]["x-upsert"] == "false"
    assert payload["bucket"] == "jt-code-assets"
    assert payload["storageKey"].startswith(f"jt-code/test/{org.id}/uploads/{user.id}/")
    assert "publicKey" not in payload


@pytest.mark.django_db
@override_settings(**STORAGE)
def test_complete_upload_verifies_content_and_persists_private_storage_identity(
    authenticated_client, org, monkeypatch
):
    monkeypatch.setattr(
        "apps.assets.views.create_signed_upload", lambda key: ("https://storage.test/upload", "token")
    )
    signature = authenticated_client.post(
        "/api/v1/files/signature/",
        {"originalFilename": "asset.png", "contentType": "image/png", "bytes": 42},
    ).json()
    monkeypatch.setattr(
        "apps.assets.views.content_checksum", lambda *args, **kwargs: ("b" * 64, "image/png", PNG)
    )
    body = {
        "uploadIntentId": signature["uploadIntentId"],
        "uploadToken": signature["uploadToken"],
        "storageKey": signature["storageKey"],
    }

    response = authenticated_client.post("/api/v1/files/complete/", body)

    assert response.status_code == 201, response.content
    asset = Asset.objects.get(storage_object_id=signature["storageKey"])
    assert asset.organization == org
    assert asset.storage_bucket == "jt-code-assets"
    assert asset.storage_url == ""
    assert asset.checksum_sha256 == "b" * 64
    assert asset.provenance["provider"] == "supabase-storage"
    assert authenticated_client.post("/api/v1/files/complete/", body).status_code == 200


@pytest.mark.django_db
@override_settings(**STORAGE, ASSET_MAX_UPLOAD_BYTES=10)
def test_signature_rejects_disallowed_types_oversize_and_wrong_storage_key(authenticated_client, monkeypatch):
    monkeypatch.setattr(
        "apps.assets.views.create_signed_upload", lambda key: ("https://storage.test/upload", "token")
    )
    svg = authenticated_client.post(
        "/api/v1/files/signature/", {"originalFilename": "x.svg", "contentType": "image/svg+xml", "bytes": 10}
    )
    assert svg.status_code == 400
    oversized = authenticated_client.post(
        "/api/v1/files/signature/",
        {"originalFilename": "x.pdf", "contentType": "application/pdf", "bytes": 11},
    )
    assert oversized.status_code == 413

    valid = authenticated_client.post(
        "/api/v1/files/signature/",
        {"originalFilename": "x.pdf", "contentType": "application/pdf", "bytes": 10},
    ).json()
    rejected = authenticated_client.post(
        "/api/v1/files/complete/",
        {
            "uploadIntentId": valid["uploadIntentId"],
            "uploadToken": valid["uploadToken"],
            "storageKey": "jt-code/other-tenant/x.pdf",
        },
    )
    assert rejected.status_code == 403
