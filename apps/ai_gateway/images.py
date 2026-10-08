"""Image generation, editing and understanding providers.

* ``gemini`` (default) - Google's Generative Language REST API: Imagen
  (``models/{GEMINI_IMAGE_MODEL}:predict``) generates images, a Gemini image
  model (``models/{GEMINI_IMAGE_EDIT_MODEL}:generateContent`` with an inline
  source image) edits them, and the vision-capable chat model describes them.
* ``echo`` - deterministic offline renderer for development and tests only;
  settings validation forbids ``AI_PROVIDER=echo`` in staging/production.

Failures raise :class:`ImageProviderError` (``retryable`` for 429/5xx/timeouts);
callers run inside ``metered(...)`` so a failed call releases its credit hold.
"""

from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from django.conf import settings

ASPECT_RATIOS = {
    "1:1": (1024, 1024),
    "16:9": (1792, 1024),
    "9:16": (1024, 1792),
    "4:3": (1365, 1024),
    "3:4": (1024, 1365),
}


class ImageProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, code: str = "IMAGE_PROVIDER_ERROR") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


@dataclass(frozen=True)
class RenderedImage:
    content: bytes
    mime_type: str
    width: int
    height: int


class ImageProvider(Protocol):
    name: str
    model_label: str

    def generate(
        self,
        prompt: str,
        *,
        count: int,
        aspect_ratio: str,
        negative_prompt: str = "",
        seed: int | None = None,
    ) -> list[RenderedImage]: ...

    def edit(self, source: bytes, source_mime: str, instruction: str) -> RenderedImage: ...

    def understand(self, source: bytes, source_mime: str, question: str) -> str: ...


def _dimensions(aspect_ratio: str) -> tuple[int, int]:
    return ASPECT_RATIOS.get(aspect_ratio, ASPECT_RATIOS["1:1"])


def _png_size(content: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(content)) as image:
        return image.size


class GeminiImageProvider:
    name = "gemini"
    model_label = "Imagen"

    def __init__(self) -> None:
        if not settings.GEMINI_API_KEY:
            raise ImageProviderError("GEMINI_API_KEY is not configured.", code="PROVIDER_NOT_CONFIGURED")
        self.base = settings.GEMINI_API_BASE.rstrip("/")
        self.headers = {"x-goog-api-key": settings.GEMINI_API_KEY, "Content-Type": "application/json"}

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(1 + max(0, settings.AI_MAX_RETRIES)):
            try:
                response = httpx.post(
                    f"{self.base}/{path}",
                    json=body,
                    headers=self.headers,
                    timeout=settings.AI_REQUEST_TIMEOUT_SECONDS,
                )
            except httpx.HTTPError as exc:
                last = ImageProviderError(
                    f"Image provider unreachable: {exc}", retryable=True, code="PROVIDER_UNAVAILABLE"
                )
            else:
                if response.status_code < 400:
                    return dict(response.json())
                retryable = response.status_code in (408, 429) or response.status_code >= 500
                last = ImageProviderError(
                    f"Image provider returned {response.status_code}: {response.text[:200]}",
                    retryable=retryable,
                    code="PROVIDER_UNAVAILABLE" if retryable else "PROVIDER_REJECTED",
                )
                if not retryable:
                    break
            time.sleep(
                min(settings.AI_RETRY_MAX_BACKOFF_SECONDS, settings.AI_RETRY_BASE_SECONDS * 2**attempt)
            )
        raise last or ImageProviderError("Image provider failed.", retryable=True)

    @staticmethod
    def _inline_images(data: dict[str, Any]) -> list[tuple[bytes, str]]:
        found = []
        for candidate in data.get("candidates") or []:
            for part in (candidate.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    found.append((base64.b64decode(inline["data"]), inline.get("mimeType", "image/png")))
        return found

    def generate(
        self,
        prompt: str,
        *,
        count: int,
        aspect_ratio: str,
        negative_prompt: str = "",
        seed: int | None = None,
    ) -> list[RenderedImage]:
        parameters: dict[str, Any] = {
            "sampleCount": count,
            "aspectRatio": aspect_ratio if aspect_ratio in ASPECT_RATIOS else "1:1",
        }
        if negative_prompt:
            parameters["negativePrompt"] = negative_prompt
        if seed is not None:
            parameters["seed"] = seed
            parameters["addWatermark"] = False  # Imagen requires this for deterministic seeds
        data = self._post(
            f"models/{settings.GEMINI_IMAGE_MODEL}:predict",
            {"instances": [{"prompt": prompt}], "parameters": parameters},
        )
        images = []
        for prediction in data.get("predictions") or []:
            if prediction.get("bytesBase64Encoded"):
                content = base64.b64decode(prediction["bytesBase64Encoded"])
                width, height = _png_size(content)
                images.append(RenderedImage(content, prediction.get("mimeType", "image/png"), width, height))
        if not images:
            raise ImageProviderError(
                "The provider returned no images (the prompt may have been filtered).", code="NO_IMAGES"
            )
        return images

    def edit(self, source: bytes, source_mime: str, instruction: str) -> RenderedImage:
        data = self._post(
            f"models/{settings.GEMINI_IMAGE_EDIT_MODEL}:generateContent",
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": source_mime,
                                    "data": base64.b64encode(source).decode(),
                                }
                            },
                            {"text": instruction},
                        ],
                    }
                ],
                "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
            },
        )
        images = self._inline_images(data)
        if not images:
            raise ImageProviderError("The provider returned no edited image.", code="NO_IMAGES")
        content, mime = images[0]
        width, height = _png_size(content)
        return RenderedImage(content, mime, width, height)

    def understand(self, source: bytes, source_mime: str, question: str) -> str:
        data = self._post(
            f"models/{settings.GEMINI_DEFAULT_MODEL}:generateContent",
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": source_mime,
                                    "data": base64.b64encode(source).decode(),
                                }
                            },
                            {"text": question},
                        ],
                    }
                ]
            },
        )
        texts = [
            part.get("text", "")
            for candidate in data.get("candidates") or []
            for part in (candidate.get("content") or {}).get("parts") or []
        ]
        answer = "".join(texts).strip()
        if not answer:
            raise ImageProviderError("The provider returned no description.", code="NO_ANSWER")
        return answer


class EchoImageProvider:
    """Offline renderer for development and tests (never deployable)."""

    name = "echo"
    model_label = "Development renderer"

    def _render(self, text: str, width: int, height: int, seed: str) -> RenderedImage:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (width, height), (24, 24, 36))
        draw = ImageDraw.Draw(image)
        for i in range(8):
            x, y = (i * 137 + len(seed)) % width, (i * 89) % height
            draw.rectangle([x, y, x + 60, y + 60], fill=((i * 31) % 255, (i * 47) % 255, (i * 53) % 255))
        draw.text((40, 40), text[:120], fill=(255, 255, 255))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return RenderedImage(buffer.getvalue(), "image/png", width, height)

    def generate(
        self,
        prompt: str,
        *,
        count: int,
        aspect_ratio: str,
        negative_prompt: str = "",
        seed: int | None = None,
    ) -> list[RenderedImage]:
        width, height = _dimensions(aspect_ratio)
        return [self._render(prompt, width, height, f"{seed}-{index}") for index in range(count)]

    def edit(self, source: bytes, source_mime: str, instruction: str) -> RenderedImage:
        return self._render(f"{instruction} (edited)", 1024, 1024, str(len(source)))

    def understand(self, source: bytes, source_mime: str, question: str) -> str:
        return f"{question} - development analysis of a {len(source)}-byte {source_mime} image."


def get_image_provider() -> ImageProvider:
    configured = (getattr(settings, "IMAGE_PROVIDER", "") or "").lower()
    if configured == "echo" or (not configured and settings.AI_PROVIDER == "echo"):
        return EchoImageProvider()
    return GeminiImageProvider()


def available_models() -> list[dict[str, str]]:
    """Models offered to the frontend (``ImageModel`` ids it understands)."""
    try:
        provider = get_image_provider()
    except ImageProviderError:
        return []
    models = [{"id": "auto", "label": "Auto (recommended)"}]
    if provider.name == "gemini":
        models.append({"id": "imagen", "label": "Imagen"})
    return models
