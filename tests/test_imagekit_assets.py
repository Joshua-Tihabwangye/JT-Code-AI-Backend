from __future__ import annotations

import hashlib
import hmac

import pytest
from django.test import override_settings

from apps.assets.models import Asset
from apps.identity.models import Organization


@pytest.fixture
def org(user):
    org = Organization.objects.create(name="ImageKit Org", slug="imagekit-org", owner=user)
    user.organizations.add(org)
    return org


@pytest.mark.django_db
@override_settings(
    IMAGEKIT_PUBLIC_KEY="public_test",
    IMAGEKIT_PRIVATE_KEY="private_test",
    IMAGEKIT_ENDPOINT_URL="https://ik.imagekit.io/jt-code",
    IMAGEKIT_UPLOAD_FOLDER="jt-code/test",
    IMAGEKIT_MAX_UPLOAD_BYTES=1024,
)
def test_imagekit_signature_endpoint_returns_scoped_auth(authenticated_client, user, org):
    response = authenticated_client.post(
        "/api/v1/files/signature/",
        {
            "originalFilename": "Quarter Report.pdf",
            "contentType": "application/pdf",
            "bytes": 512,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["publicKey"] == "public_test"
    assert payload["folder"] == f"/jt-code/test/{user.id}"
    assert payload["fileName"] == "Quarter_Report.pdf"
    expected = hmac.new(
        b"private_test",
        f"{payload['token']}{payload['expire']}".encode(),
        hashlib.sha1,
    ).hexdigest()
    assert payload["signature"] == expected


@pytest.mark.django_db
@override_settings(
    IMAGEKIT_PUBLIC_KEY="public_test",
    IMAGEKIT_PRIVATE_KEY="private_test",
    IMAGEKIT_ENDPOINT_URL="https://ik.imagekit.io/jt-code",
    IMAGEKIT_UPLOAD_FOLDER="jt-code/test",
)
def test_complete_upload_verifies_imagekit_file_before_persisting(
    authenticated_client,
    user,
    org,
    monkeypatch,
):
    file_path = f"/jt-code/test/{user.id}/asset.png"

    def fake_verify(file_id):
        assert file_id == "file_123"
        return {
            "fileId": "file_123",
            "filePath": file_path,
            "url": "https://ik.imagekit.io/jt-code/asset.png",
            "fileType": "image",
            "size": 42,
            "format": "png",
            "thumbnailUrl": "https://ik.imagekit.io/jt-code/tr:n-thumb/asset.png",
        }

    monkeypatch.setattr("apps.assets.views.verify_imagekit_file", fake_verify)

    response = authenticated_client.post(
        "/api/v1/files/complete/",
        {
            "fileId": "file_123",
            "filePath": file_path,
            "url": "https://ik.imagekit.io/jt-code/asset.png",
            "fileType": "image",
            "format": "png",
            "size": 42,
            "originalFilename": "asset.png",
        },
    )

    assert response.status_code == 201, response.content
    asset = Asset.objects.get(imagekit_file_id="file_123")
    assert asset.organization == org
    assert asset.imagekit_file_path == file_path
    assert asset.secure_url == "https://ik.imagekit.io/jt-code/asset.png"


@pytest.mark.django_db
@override_settings(
    IMAGEKIT_PUBLIC_KEY="public_test",
    IMAGEKIT_PRIVATE_KEY="private_test",
    IMAGEKIT_ENDPOINT_URL="https://ik.imagekit.io/jt-code",
    IMAGEKIT_UPLOAD_FOLDER="jt-code/test",
)
def test_complete_upload_rejects_wrong_user_folder(authenticated_client, user, org):
    response = authenticated_client.post(
        "/api/v1/files/complete/",
        {
            "fileId": "file_123",
            "filePath": "/jt-code/test/someone-else/asset.png",
            "url": "https://ik.imagekit.io/jt-code/asset.png",
            "fileType": "image",
            "format": "png",
            "size": 42,
            "originalFilename": "asset.png",
        },
    )

    assert response.status_code == 403
