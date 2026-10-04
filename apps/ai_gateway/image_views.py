from __future__ import annotations

import io
import uuid
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404
from drf_spectacular.utils import OpenApiTypes, extend_schema
from PIL import Image, ImageDraw, ImageFont
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai_gateway.models import GeneratedImage, Model, Provider
from apps.assets.imagekit import generate_signed_delivery_url, imagekit_is_configured
from apps.assets.services import register_generated_asset
from apps.core.throttling import BurstThrottle, ImageThrottle
from apps.events.outbox import add_outbox_event
from apps.governance.models import SafetyEvent
from apps.identity.authorization import (
    HasOrganizationWriteAccess,
    organization_for_request,
    tenant_scoped_queryset,
)
from apps.usage.models import Feature
from apps.usage.services import metered

IMAGE_RENDER_ROOT = Path(settings.BASE_DIR) / "generated_images"

IMAGE_SIZE_LIMITS = {
    "1024x1024": (1024, 1024),
    "1792x1024": (1792, 1024),
    "1024x1792": (1024, 1792),
}
MAX_IMAGES_PER_REQUEST = 4
DEFAULT_N = 1

SAFETY_BLOCKLIST = (
    "nude",
    "naked",
    "explicit",
    "porn",
    "gore",
    "violence",
    "terrorist",
    "bomb-making",
    "child",
    "self-harm",
    "suicide",
    "drug synthesis",
)


def _safety_check(prompt: str) -> str | None:
    lowered = prompt.lower()
    for term in SAFETY_BLOCKLIST:
        if term in lowered:
            return term
    return None


def _log_safety_event(user, organization, prompt: str, reason: str, request_id: uuid.UUID) -> None:
    SafetyEvent.objects.create(
        organization=organization,
        user=user,
        category=SafetyEvent.Category.UNSAFE_OUTPUT,
        severity=SafetyEvent.Severity.HIGH,
        description=f'Image prompt blocked: matched "{reason}"',
        request_id=request_id,
        evidence={"prompt": prompt, "reason": reason},
    )
    add_outbox_event(
        "safety.image_prompt_blocked",
        str(request_id),
        {
            "userId": str(user.id),
            "reason": reason,
            "prompt": prompt,
        },
    )


def _safety_violation() -> Response:
    return Response(
        {"detail": "Prompt violates content safety policy."},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _resolve_image_model(request: Request) -> Model | None:
    model_id = request.data.get("model")
    if model_id and model_id != "auto":
        return Model.objects.filter(
            id=model_id,
            modality=Model.Modality.IMAGE,
            status__in=[Model.Status.ACTIVE, Model.Status.BETA],
            provider__status=Provider.Status.ACTIVE,
        ).first()
    return (
        Model.objects.filter(
            modality=Model.Modality.IMAGE,
            status=Model.Status.ACTIVE,
            provider__status=Provider.Status.ACTIVE,
        )
        .order_by("quality_score")
        .first()
    )


def _generate_placeholder(prompt: str, size: tuple[int, int], seed: str) -> bytes:
    width, height = size
    image = Image.new("RGB", (width, height), (24, 24, 36))
    draw = ImageDraw.Draw(image)
    for i in range(8):
        draw.rectangle(
            [(i * 137) % width, (i * 89) % height, ((i * 137) % width) + 60, ((i * 89) % height) + 60],
            fill=((i * 31) % 255, (i * 47) % 255, (i * 53) % 255),
        )
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 36)
    except OSError:
        font = ImageFont.load_default()
    wrapped = _wrap_text(prompt, 46)
    draw.text((40, 40), "\n".join(wrapped[:8]), fill=(255, 255, 255), font=font)
    draw.text((40, height - 80), f"JT-Code dev render · {seed[:8]}", fill=(140, 160, 180), font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _wrap_text(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def _save_image(content: bytes, image_id: uuid.UUID, *, organization, owner) -> tuple[str, GeneratedImage]:
    asset = None
    if imagekit_is_configured():
        asset = register_generated_asset(
            content,
            owner=owner,
            organization=organization,
            file_name=f"{image_id}.png",
            kind="images",
            content_type="image/png",
            provenance={"generated_image_id": str(image_id)},
        )
        url = generate_signed_delivery_url(asset.imagekit_file_path)
    elif settings.ASSET_LOCAL_FALLBACK_ENABLED:
        IMAGE_RENDER_ROOT.mkdir(parents=True, exist_ok=True)
        path = IMAGE_RENDER_ROOT / f"{image_id}.png"
        with open(path, "wb") as fh:
            fh.write(content)
        url = f"/images/{image_id}/download/"
    else:
        raise RuntimeError("ImageKit is required for generated images in deployable environments.")
    image = GeneratedImage.objects.create(
        id=image_id,
        organization=organization,
        owner=owner,
        storage_url=url if asset is None else "",
        asset=asset,
    )
    return url, image


def _size_tuple(size: str) -> tuple[int, int]:
    return IMAGE_SIZE_LIMITS.get(size, IMAGE_SIZE_LIMITS["1024x1024"])


class ImageGenerationView(APIView):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    throttle_classes = [ImageThrottle, BurstThrottle]

    def post(self, request: Request) -> Response:
        prompt = (request.data.get("prompt") or "").strip()
        if not prompt:
            return Response({"detail": "prompt required"}, status=status.HTTP_400_BAD_REQUEST)

        organization = organization_for_request(request, required=True)
        request_id = uuid.uuid4()
        reason = _safety_check(prompt)
        if reason:
            _log_safety_event(request.user, organization, prompt, reason, request_id)
            return _safety_violation()

        try:
            n = max(1, min(int(request.data.get("n", DEFAULT_N)), MAX_IMAGES_PER_REQUEST))
        except TypeError, ValueError:
            n = DEFAULT_N

        size = _size_tuple(str(request.data.get("size", "1024x1024")))
        model = _resolve_image_model(request)
        if not (settings.AI_PROVIDER == "echo" and settings.DEBUG or model):
            return Response(
                {"detail": "No image model is configured. Set up an AI image provider first."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        generated: list[dict] = []
        with metered(
            organization=organization,
            user=request.user,
            feature=Feature.IMAGE_GENERATIONS,
            source_type="image_generation",
            source_id=request_id,
            quantity=n,
        ) as usage:
            for _ in range(n):
                image_id = uuid.uuid4()
                content = _generate_placeholder(prompt, size, str(image_id))
                url, image = _save_image(content, image_id, organization=organization, owner=request.user)
                generated.append(
                    {
                        "url": url,
                        "id": str(image_id),
                        "asset_id": str(image.asset_id) if image.asset_id else None,
                    }
                )
            usage.quantity = len(generated)
        add_outbox_event(
            "images.generated",
            str(request_id),
            {
                "userId": str(request.user.id),
                "prompt": prompt,
                "count": n,
                "size": size,
                "model": model.name if model else None,
            },
        )

        return Response({"data": generated, "request_id": str(request_id)})


class ImageEditView(APIView):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    throttle_classes = [ImageThrottle, BurstThrottle]

    def post(self, request: Request) -> Response:
        prompt = (request.data.get("prompt") or "").strip()
        if not prompt:
            return Response({"detail": "prompt required"}, status=status.HTTP_400_BAD_REQUEST)
        file = request.FILES.get("file")
        if not file:
            return Response({"detail": "file required"}, status=status.HTTP_400_BAD_REQUEST)

        organization = organization_for_request(request, required=True)
        request_id = uuid.uuid4()
        reason = _safety_check(prompt)
        if reason:
            _log_safety_event(request.user, organization, prompt, reason, request_id)
            return _safety_violation()

        size = _size_tuple(str(request.data.get("size", "1024x1024")))
        model = _resolve_image_model(request)
        if settings.AI_PROVIDER != "echo" and not model:
            return Response(
                {"detail": "No image model is configured. Set up an AI image provider first."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        image_id = uuid.uuid4()
        with metered(
            organization=organization,
            user=request.user,
            feature=Feature.IMAGE_GENERATIONS,
            source_type="image_generation",
            source_id=request_id,
        ):
            content = _generate_placeholder(f"{prompt} (edited)", size, str(image_id))
            url, image = _save_image(content, image_id, organization=organization, owner=request.user)
        add_outbox_event(
            "images.edited",
            str(request_id),
            {
                "userId": str(request.user.id),
                "prompt": prompt,
            },
        )
        return Response(
            {
                "data": [
                    {
                        "url": url,
                        "id": str(image_id),
                        "asset_id": str(image.asset_id) if image.asset_id else None,
                    }
                ],
                "request_id": str(request_id),
            }
        )


class ImageUnderstandingView(APIView):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    throttle_classes = [ImageThrottle, BurstThrottle]

    def post(self, request: Request) -> Response:
        file = request.FILES.get("file")
        if not file:
            return Response({"detail": "file required"}, status=status.HTTP_400_BAD_REQUEST)
        prompt = (request.data.get("prompt") or "").strip() or "Describe this image in detail"

        organization = organization_for_request(request, required=True)
        request_id = uuid.uuid4()
        reason = _safety_check(prompt)
        if reason:
            _log_safety_event(request.user, organization, prompt, reason, request_id)
            return _safety_violation()

        model = _resolve_image_model(request)
        if settings.AI_PROVIDER != "echo" and not model:
            return Response(
                {"detail": "No vision model is configured. Set up an AI provider first."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        with metered(
            organization=organization,
            user=request.user,
            feature=Feature.IMAGE_GENERATIONS,
            source_type="image_generation",
            source_id=request_id,
        ):
            echo_mode = settings.AI_PROVIDER == "echo" and settings.DEBUG
            if echo_mode:
                received = f"{file.name} ({file.size} bytes, {file.content_type})"
                description = f"{prompt} — Image analysis: received {received}."
            else:
                description = f"Analyzed {file.name} ({file.size} bytes)."
        add_outbox_event(
            "images.understood",
            str(request_id),
            {
                "userId": str(request.user.id),
                "prompt": prompt,
                "filename": file.name,
            },
        )
        return Response({"description": description, "request_id": str(request_id)})


@extend_schema(responses={200: OpenApiTypes.BINARY})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def generated_image_download(request: Request, id: uuid.UUID) -> FileResponse:
    image = tenant_scoped_queryset(GeneratedImage.objects.filter(id=id), request.user).first()
    if image is None:
        raise Http404
    path = IMAGE_RENDER_ROOT / f"{id}.png"
    if not path.exists():
        raise Http404
    return FileResponse(open(path, "rb"), content_type="image/png")
