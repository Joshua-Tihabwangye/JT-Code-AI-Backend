"""P17-7: images are generated with a Gemini image model (generateContent) by default.

Google has shut down Imagen (``:predict``) in the Gemini API, so the shipped default must not use it.
"""

from __future__ import annotations

import base64
import struct
import zlib

import httpx

from apps.ai_gateway import images


def _png(width: int = 8, height: int = 4) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def test_default_image_models_are_gemini_models_not_imagen():
    from django.conf import settings

    assert not settings.GEMINI_IMAGE_MODEL.startswith("imagen")
    assert not settings.GEMINI_IMAGE_EDIT_MODEL.startswith("imagen")
    assert settings.GEMINI_DEFAULT_MODEL not in {"gemini-2.0-flash", "gemini-2.5-flash"}  # shut down


def test_generation_calls_generate_content_once_per_image(settings, monkeypatch):
    settings.GEMINI_API_KEY = "gemini-test-key"  # pragma: allowlist secret
    settings.GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image"
    png = base64.b64encode(_png()).decode()
    calls = []

    def fake_post(url, json, headers, timeout):  # noqa: A002
        calls.append((url, json))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": png}}]}}
                ]
            },
        )

    monkeypatch.setattr(images.httpx, "post", fake_post)
    result = images.GeminiImageProvider().generate(
        "a square", count=2, aspect_ratio="16:9", negative_prompt="text", seed=7
    )
    assert len(result) == 2 and result[0].width == 8 and result[0].height == 4
    assert [url.rsplit("/", 1)[1] for url, _ in calls] == ["gemini-3.1-flash-image:generateContent"] * 2
    body = calls[0][1]
    assert body["contents"][0]["parts"][0]["text"] == "a square\n\nAvoid: text"
    assert body["generationConfig"]["responseModalities"] == ["IMAGE"]
    assert body["generationConfig"]["imageConfig"] == {"aspectRatio": "16:9"}
    assert [c[1]["generationConfig"]["seed"] for c in calls] == [7, 8]
    assert all(":predict" not in url for url, _ in calls)


def test_a_filtered_prompt_is_reported_not_returned_empty(settings, monkeypatch):
    settings.GEMINI_API_KEY = "gemini-test-key"  # pragma: allowlist secret
    settings.GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image"
    monkeypatch.setattr(
        images.httpx, "post", lambda url, json, headers, timeout: httpx.Response(200, json={"candidates": []})
    )
    try:
        images.GeminiImageProvider().generate("x", count=1, aspect_ratio="1:1")
    except images.ImageProviderError as exc:
        assert exc.code == "NO_IMAGES"
    else:
        raise AssertionError("expected NO_IMAGES")
