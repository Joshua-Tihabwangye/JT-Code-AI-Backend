"""Direct (browser → ImageKit V2) upload authorization and server-side completion."""

from __future__ import annotations

import jwt
import pytest
from django.test import override_settings

from apps.assets.models import Asset
from apps.identity.models import Organization

IMAGEKIT = {
    "IMAGEKIT_PUBLIC_KEY": "public_test",
    "IMAGEKIT_PRIVATE_KEY": "private_test",
    "IMAGEKIT_ENDPOINT_URL": "https://ik.imagekit.io/jt-code",
    "IMAGEKIT_UPLOAD_FOLDER": "jt-code/test",
}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 34


@pytest.fixture
def org(user):
    org = Organization.objects.create(name="ImageKit Org", slug="imagekit-org", owner=user)
    user.organizations.add(org)
    return org


@pytest.mark.django_db
@override_settings(**IMAGEKIT, IMAGEKIT_MAX_UPLOAD_BYTES=1024)
def test_signature_issues_v2_jwt_binding_every_upload_parameter(authenticated_client, user, org):
    response = authenticated_client.post(
        "/api/v1/files/signature/",
        {"originalFilename": "Quarter Report.pdf", "contentType": "application/pdf", "bytes": 512},
    )
    assert response.status_code == 200, response.content
    payload = response.json()
    assert payload["uploadUrl"] == "https://upload.imagekit.io/api/v2/files/upload"
    assert payload["folder"] == f"/jt-code/test/{org.id}/uploads/{user.id}"
    assert payload["fileName"].endswith("-Quarter_Report.pdf")
    header = jwt.get_unverified_header(payload["token"])
    assert header == {"alg": "HS256", "typ": "JWT", "kid": "public_test"}
    claims = jwt.decode(payload["token"], "private_test", algorithms=["HS256"])
    params = payload["uploadParams"]
    for key, value in params.items():
        assert claims[key] == value
    assert params["isPrivateFile"] == "true"
    assert params["overwriteFile"] == "false"
    assert params["checks"] == '"file.size" = 512'
    assert claims["exp"] - claims["iat"] <= 3600
    assert payload["uploadToken"] and payload["uploadToken"] != payload["token"]


@pytest.mark.django_db
def test_signature_rejects_disallowed_types_and_oversize(authenticated_client, org, settings):
    # Only the settings fixture here: mixing it with @override_settings restores
    # overrides out of order and leaks settings into later tests.
    for name, value in IMAGEKIT.items():
        setattr(settings, name, value)
    svg = authenticated_client.post(
        "/api/v1/files/signature/", {"originalFilename": "x.svg", "contentType": "image/svg+xml", "bytes": 10}
    )
    assert svg.status_code == 400
    settings.IMAGEKIT_MAX_UPLOAD_BYTES = 10
    big = authenticated_client.post(
        "/api/v1/files/signature/",
        {"originalFilename": "x.pdf", "contentType": "application/pdf", "bytes": 11},
    )
    assert big.status_code == 413


def _intent(client):
    return client.post(
        "/api/v1/files/signature/", {"originalFilename": "asset.png", "contentType": "image/png", "bytes": 42}
    ).json()


def _details(path, **overrides):
    return {
        "fileId": "file_123",
        "filePath": path,
        "url": "https://ik.imagekit.io/jt-code/asset.png",
        "fileType": "image",
        "size": 42,
        "format": "png",
        "isPrivateFile": True,
        "versionInfo": {"id": "v1", "name": "Version 1"},
        **overrides,
    }


@pytest.mark.django_db
@override_settings(**IMAGEKIT)
def test_complete_upload_verifies_identity_bytes_and_type_before_persisting(
    authenticated_client, org, monkeypatch
):
    signature = _intent(authenticated_client)
    file_path = f"{signature['folder']}/{signature['fileName']}"
    monkeypatch.setattr("apps.assets.views.verify_imagekit_file", lambda file_id: _details(file_path))
    monkeypatch.setattr("apps.assets.views.content_checksum", lambda *a, **k: ("b" * 64, "image/png", PNG))
    body = {
        "uploadIntentId": signature["uploadIntentId"],
        "uploadToken": signature["uploadToken"],
        "fileId": "file_123",
        "filePath": file_path,
    }

    response = authenticated_client.post("/api/v1/files/complete/", body)

    assert response.status_code == 201, response.content
    asset = Asset.objects.get(imagekit_file_id="file_123")
    assert asset.organization == org
    assert asset.visibility == Asset.Visibility.PRIVATE
    assert asset.checksum_sha256 == "b" * 64
    assert asset.provenance["fingerprintVersion"] == 2
    assert response.json()["name"] == "asset.png" and response.json()["mimeType"] == "image/png"
    repeated = authenticated_client.post("/api/v1/files/complete/", body)
    assert repeated.status_code == 200
    assert Asset.objects.filter(imagekit_file_id="file_123").count() == 1


@pytest.mark.django_db
@override_settings(**IMAGEKIT)
def test_complete_upload_rejects_bytes_that_do_not_match_the_declared_type(
    authenticated_client, org, monkeypatch
):
    signature = _intent(authenticated_client)
    file_path = f"{signature['folder']}/{signature['fileName']}"
    monkeypatch.setattr("apps.assets.views.verify_imagekit_file", lambda file_id: _details(file_path))
    monkeypatch.setattr(
        "apps.assets.views.content_checksum", lambda *a, **k: ("b" * 64, "image/png", b"<html><script>")
    )
    response = authenticated_client.post(
        "/api/v1/files/complete/",
        {
            "uploadIntentId": signature["uploadIntentId"],
            "uploadToken": signature["uploadToken"],
            "fileId": "file_123",
            "filePath": file_path,
        },
    )
    assert response.status_code == 409
    assert not Asset.objects.exists()


@pytest.mark.django_db
@override_settings(**IMAGEKIT)
def test_complete_upload_rejects_wrong_folder_public_files_and_bad_tokens(
    authenticated_client, org, monkeypatch
):
    signature = _intent(authenticated_client)
    file_path = f"{signature['folder']}/{signature['fileName']}"
    base = {"uploadIntentId": signature["uploadIntentId"], "fileId": "file_123"}
    wrong_folder = authenticated_client.post(
        "/api/v1/files/complete/",
        {**base, "uploadToken": signature["uploadToken"], "filePath": "/jt-code/test/someone-else/asset.png"},
    )
    assert wrong_folder.status_code == 403
    bad_token = authenticated_client.post(
        "/api/v1/files/complete/", {**base, "uploadToken": signature["token"][:100], "filePath": file_path}
    )
    assert bad_token.status_code == 403
    monkeypatch.setattr(
        "apps.assets.views.verify_imagekit_file", lambda file_id: _details(file_path, isPrivateFile=False)
    )
    public = authenticated_client.post(
        "/api/v1/files/complete/", {**base, "uploadToken": signature["uploadToken"], "filePath": file_path}
    )
    assert public.status_code == 409
