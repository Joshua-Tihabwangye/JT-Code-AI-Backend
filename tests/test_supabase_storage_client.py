"""REST-boundary tests for the private Supabase Storage adapter."""

from __future__ import annotations

from apps.assets import supabase_storage


def _configure(settings):
    settings.SUPABASE_URL = "https://project.supabase.co"
    settings.SUPABASE_INTERNAL_URL = ""
    settings.SUPABASE_SECRET_KEY = "sb_secret_test"
    settings.SUPABASE_STORAGE_BUCKET = "jt-code-assets"
    settings.SUPABASE_STORAGE_PREFIX = "jt-code/test"
    settings.SUPABASE_STORAGE_API_URL = ""
    settings.SUPABASE_STORAGE_PUBLIC_API_URL = ""
    settings.SUPABASE_STORAGE_TIMEOUT_SECONDS = 5
    settings.ASSET_MAX_UPLOAD_BYTES = 1024
    settings.ASSET_SIGNED_URL_TTL_SECONDS = 60
    settings.ASSET_ALLOWED_CONTENT_TYPES = ("image/png", "application/pdf")


class Response:
    status_code = 200

    def __init__(self, payload=None):
        self.payload = payload or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_create_signed_upload_uses_service_key_only_server_side(settings, monkeypatch):
    _configure(settings)
    monkeypatch.setattr(supabase_storage, "ensure_private_bucket", lambda: None)
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response({"token": "one-object-token", "url": "/object/upload/sign/jt-code-assets/path.png"})

    monkeypatch.setattr(supabase_storage.httpx, "post", post)
    url, token = supabase_storage.create_signed_upload("jt-code/test/org/uploads/path.png")

    assert token == "one-object-token"
    assert url.endswith("/object/upload/sign/jt-code-assets/jt-code/test/org/uploads/path.png")
    assert captured["headers"]["Authorization"] == "Bearer sb_secret_test"
    assert captured["headers"]["apikey"] == "sb_secret_test"
    assert captured["json"] == {"upsert": "false"}


def test_server_upload_is_private_non_overwriting_and_tenant_prefixed(settings, monkeypatch):
    _configure(settings)
    monkeypatch.setattr(supabase_storage, "ensure_private_bucket", lambda: None)
    monkeypatch.setattr(supabase_storage.uuid, "uuid4", lambda: type("Id", (), {"hex": "a" * 32})())
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response({"Key": "unused"})

    monkeypatch.setattr(supabase_storage.httpx, "post", post)
    resource = supabase_storage.upload_bytes(
        b"png-bytes", file_name="chart.png", folder="jt-code/test/org/charts", content_type="image/png"
    )

    assert resource == {
        "bucket": "jt-code-assets",
        "key": "jt-code/test/org/charts/aaaaaaaaaaaa-chart.png",
        "size": 9,
        "content_type": "image/png",
    }
    assert captured["headers"]["x-upsert"] == "false"
    assert captured["headers"]["cache-control"].startswith("private")
    assert captured["content"] == b"png-bytes"


def test_signed_delivery_uses_expiring_private_object_route(settings, monkeypatch):
    _configure(settings)
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response({"signedURL": "/object/sign/jt-code-assets/path.pdf?token=temporary"})

    monkeypatch.setattr(supabase_storage.httpx, "post", post)
    url = supabase_storage.generate_signed_delivery_url("jt-code/test/org/path.pdf")

    assert url == "https://project.supabase.co/storage/v1/object/sign/jt-code-assets/path.pdf?token=temporary"
    assert captured["json"] == {"expiresIn": 60}


def test_server_uses_internal_storage_but_returns_the_public_gateway(settings, monkeypatch):
    _configure(settings)
    settings.SUPABASE_INTERNAL_URL = "http://supabase-gateway:8000"
    settings.SUPABASE_STORAGE_PUBLIC_API_URL = "http://localhost:54321/storage/v1"
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response({"signedURL": "/object/sign/jt-code-assets/path.pdf?token=temporary"})

    monkeypatch.setattr(supabase_storage.httpx, "post", post)
    url = supabase_storage.generate_signed_delivery_url("jt-code/test/org/path.pdf")

    assert captured["url"].startswith("http://supabase-gateway:8000/storage/v1/")
    assert url.startswith("http://localhost:54321/storage/v1/")
