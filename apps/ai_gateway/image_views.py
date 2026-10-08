from __future__ import annotations

import uuid
from pathlib import Path

from django.conf import settings
from django.db.models import Q
from django.http import FileResponse, Http404
from drf_spectacular.utils import OpenApiTypes, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai_gateway.images import ImageProviderError, get_image_provider
from apps.ai_gateway.models import GeneratedImage, ImageGeneration, Model, Provider
from apps.assets.services import register_generated_asset
from apps.assets.supabase_storage import generate_signed_delivery_url, supabase_storage_is_configured
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


def _save_image(
    content: bytes,
    image_id: uuid.UUID,
    *,
    organization,
    owner,
    generation: ImageGeneration | None = None,
    mime_type: str = "image/png",
    width: int = 0,
    height: int = 0,
) -> tuple[str, GeneratedImage]:
    asset = None
    extension = "jpg" if mime_type == "image/jpeg" else "png"
    if supabase_storage_is_configured():
        asset = register_generated_asset(
            content,
            owner=owner,
            organization=organization,
            file_name=f"{image_id}.{extension}",
            kind="images",
            content_type=mime_type,
            provenance={"generated_image_id": str(image_id)},
        )
        url = generate_signed_delivery_url(asset.storage_key)
    elif settings.ASSET_LOCAL_FALLBACK_ENABLED:
        IMAGE_RENDER_ROOT.mkdir(parents=True, exist_ok=True)
        path = IMAGE_RENDER_ROOT / f"{image_id}.png"
        with open(path, "wb") as fh:
            fh.write(content)
        url = f"/images/{image_id}/download/"
    else:
        raise RuntimeError("Supabase Storage is required for generated images in deployable environments.")
    image = GeneratedImage.objects.create(
        id=image_id,
        organization=organization,
        owner=owner,
        storage_url=url if asset is None else "",
        asset=asset,
        generation=generation,
        width=width,
        height=height,
    )
    return url, image


MAX_SOURCE_IMAGE_BYTES = 10 * 1024 * 1024


class ImageRefError(ValueError):
    pass


def _provider_failure(exc: ImageProviderError) -> Response:
    return Response(
        {"detail": str(exc), "code": exc.code},
        status=status.HTTP_503_SERVICE_UNAVAILABLE
        if exc.retryable or exc.code.endswith("NOT_CONFIGURED")
        else status.HTTP_502_BAD_GATEWAY,
    )


def _image_bytes(image: GeneratedImage) -> tuple[bytes, str]:
    from apps.assets.supabase_storage import stream_file

    if image.asset is not None:
        chunks, total = [], 0
        for chunk in stream_file(image.asset.storage_key):
            total += len(chunk)
            if total > MAX_SOURCE_IMAGE_BYTES:
                raise ImageRefError("Image too large.")
            chunks.append(chunk)
        return b"".join(chunks), image.asset.content_type or "image/png"
    path = IMAGE_RENDER_ROOT / f"{image.id}.png"
    if not path.exists():
        raise ImageRefError("The referenced image is no longer available.")
    return path.read_bytes(), "image/png"


def load_image_ref(ref: str, user, organization) -> tuple[bytes, str]:
    """Resolve a data URL, a generated image id or a file (asset) id the user may read."""
    import base64
    import binascii

    from apps.assets.access import assets_visible_to
    from apps.assets.models import Asset

    if ref.startswith("data:"):
        header, _, payload = ref.partition(",")
        mime = header[5:].split(";", 1)[0] or "image/png"
        if not mime.startswith("image/") or ";base64" not in header:
            raise ImageRefError("Only base64 image data URLs are accepted.")
        try:
            content = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageRefError("Invalid image data URL.") from exc
        if len(content) > MAX_SOURCE_IMAGE_BYTES:
            raise ImageRefError("Image too large.")
        return content, mime
    try:
        ref_id = uuid.UUID(ref)
    except ValueError as exc:
        raise ImageRefError("image must be a data URL, an image id or a file id.") from exc
    generated = (
        GeneratedImage.objects.select_related("asset")
        .filter(Q(id=ref_id) | Q(generation_id=ref_id), organization=organization, owner=user)
        .first()
    )
    if generated is not None:
        return _image_bytes(generated)
    asset = assets_visible_to(user, organization.id).filter(id=ref_id, status=Asset.Status.READY).first()
    if asset is None or not (asset.content_type or "").startswith("image/"):
        raise ImageRefError("The referenced image was not found.")
    image = GeneratedImage(id=ref_id, asset=asset)
    return _image_bytes(image)


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
        try:
            provider = get_image_provider()
        except ImageProviderError as exc:
            return _provider_failure(exc)
        aspect = {"1792x1024": "16:9", "1024x1792": "9:16"}.get(f"{size[0]}x{size[1]}", "1:1")

        generated: list[dict] = []
        with metered(
            organization=organization,
            user=request.user,
            feature=Feature.IMAGE_GENERATIONS,
            source_type="image_generation",
            source_id=request_id,
            quantity=n,
        ) as usage:
            try:
                rendered = provider.generate(prompt, count=n, aspect_ratio=aspect)
            except ImageProviderError as exc:
                usage.quantity = 0
                return _provider_failure(exc)
            for item in rendered:
                image_id = uuid.uuid4()
                url, image = _save_image(
                    item.content,
                    image_id,
                    organization=organization,
                    owner=request.user,
                    mime_type=item.mime_type,
                    width=item.width,
                    height=item.height,
                )
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

        try:
            provider = get_image_provider()
        except ImageProviderError as exc:
            return _provider_failure(exc)
        image_id = uuid.uuid4()
        with metered(
            organization=organization,
            user=request.user,
            feature=Feature.IMAGE_GENERATIONS,
            source_type="image_generation",
            source_id=request_id,
        ) as usage:
            try:
                rendered = provider.edit(file.read(), file.content_type or "image/png", prompt)
            except ImageProviderError as exc:
                usage.quantity = 0
                return _provider_failure(exc)
            url, image = _save_image(
                rendered.content,
                image_id,
                organization=organization,
                owner=request.user,
                mime_type=rendered.mime_type,
                width=rendered.width,
                height=rendered.height,
            )
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
    """Describe an image: multipart ``file`` + ``prompt``, or JSON ``{image, question}``
    where ``image`` is a data URL, a generated image id or a file (asset) id."""

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    throttle_classes = [ImageThrottle, BurstThrottle]

    def post(self, request: Request) -> Response:
        organization = organization_for_request(request, required=True)
        file = request.FILES.get("file")
        prompt = (request.data.get("prompt") or request.data.get("question") or "").strip()
        prompt = prompt or "Describe this image in detail"
        try:
            if file is not None:
                if file.size > MAX_SOURCE_IMAGE_BYTES:
                    return Response(
                        {"detail": "Image too large."}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
                    )
                source, mime = file.read(), file.content_type or "image/png"
            elif request.data.get("image"):
                source, mime = load_image_ref(str(request.data["image"]), request.user, organization)
            else:
                return Response({"detail": "file or image required"}, status=status.HTTP_400_BAD_REQUEST)
        except ImageRefError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        request_id = uuid.uuid4()
        reason = _safety_check(prompt)
        if reason:
            _log_safety_event(request.user, organization, prompt, reason, request_id)
            return _safety_violation()
        try:
            provider = get_image_provider()
        except ImageProviderError as exc:
            return _provider_failure(exc)
        with metered(
            organization=organization,
            user=request.user,
            feature=Feature.IMAGE_GENERATIONS,
            source_type="image_generation",
            source_id=request_id,
        ) as usage:
            try:
                description = provider.understand(source, mime, prompt)
            except ImageProviderError as exc:
                usage.quantity = 0  # releases the hold: nothing was produced
                return _provider_failure(exc)
            ImageGeneration.objects.create(
                organization=organization,
                owner=request.user,
                mode=ImageGeneration.Mode.UNDERSTAND,
                prompt=prompt,
                provider=provider.name,
                image_count=0,
                answer=description,
            )
        add_outbox_event(
            "images.understood",
            str(request_id),
            {"userId": str(request.user.id), "prompt": prompt, "filename": getattr(file, "name", "")},
        )
        return Response({"description": description, "answer": description, "request_id": str(request_id)})


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
