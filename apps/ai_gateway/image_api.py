"""The frontend image contract (``/images/...``) over :mod:`apps.ai_gateway.images`.

``POST /images/generate/`` and ``/images/edit/`` return the frontend
``ImageGeneration`` shape; ``/images/`` lists the caller's generations (the
gallery), with favorite, delete and save-to-files. Every call is safety-checked
and metered; a provider failure releases the credit hold and returns 503/502.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from drf_spectacular.utils import OpenApiTypes, extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.ai_gateway.image_views import (
    MAX_IMAGES_PER_REQUEST,
    ImageRefError,
    _log_safety_event,
    _provider_failure,
    _safety_check,
    _safety_violation,
    _save_image,
    load_image_ref,
)
from apps.ai_gateway.images import ImageProviderError, available_models, get_image_provider
from apps.ai_gateway.models import GeneratedImage, ImageGeneration
from apps.assets.services import soft_delete_asset
from apps.assets.supabase_storage import generate_signed_delivery_url
from apps.core.throttling import BurstThrottle, ImageThrottle
from apps.core.views import APIView
from apps.events.outbox import add_outbox_event
from apps.identity.authorization import HasOrganizationWriteAccess, organization_for_request
from apps.usage.models import Feature
from apps.usage.services import metered


def _image_url(image: GeneratedImage) -> str:
    if image.asset is not None:
        return generate_signed_delivery_url(image.asset.storage_key)
    return image.storage_url


def serialize_generation(generation: ImageGeneration) -> dict[str, Any]:
    return {
        "id": str(generation.id),
        "prompt": generation.prompt,
        "negativePrompt": generation.negative_prompt or None,
        "model": generation.model,
        "aspectRatio": generation.aspect_ratio,
        "style": generation.style,
        "seed": generation.seed,
        "imageCount": generation.image_count,
        "createdAt": generation.created_at.isoformat(),
        "images": [
            {"id": str(image.id), "url": _image_url(image), "width": image.width, "height": image.height}
            for image in generation.images.select_related("asset").order_by("created_at")
        ],
        "favorite": generation.favorite,
        "mode": generation.mode,
        "answer": generation.answer or None,
    }


def _options(data: Any) -> dict[str, Any]:
    try:
        count = max(1, min(int(data.get("imageCount", 1)), MAX_IMAGES_PER_REQUEST))
    except TypeError, ValueError:
        count = 1
    seed = data.get("seed")
    try:
        seed = int(seed) if seed not in (None, "") else None
    except TypeError, ValueError:
        seed = None
    model = str(data.get("model") or "auto")
    return {
        "model": model if model in {m["id"] for m in available_models()} else "auto",
        "aspect_ratio": str(data.get("aspectRatio") or "1:1")[:10],
        "style": str(data.get("style") or "")[:80],
        "negative_prompt": str(data.get("negativePrompt") or "")[:2000],
        "seed": seed,
        "count": count,
    }


class _ImageWriteView(APIView):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    throttle_classes = [ImageThrottle, BurstThrottle]

    def _run(self, request: Request, *, mode: str, prompt: str, render) -> Response:  # type: ignore[no-untyped-def]
        organization = organization_for_request(request, required=True)
        request_id = uuid.uuid4()
        reason = _safety_check(f"{prompt} {request.data.get('negativePrompt') or ''}")
        if reason:
            _log_safety_event(request.user, organization, prompt, reason, request_id)
            return _safety_violation()
        try:
            provider = get_image_provider()
        except ImageProviderError as exc:
            return _provider_failure(exc)
        options = _options(request.data)
        quantity = options["count"] if mode == ImageGeneration.Mode.GENERATE else 1
        with metered(
            organization=organization,
            user=request.user,
            feature=Feature.IMAGE_GENERATIONS,
            source_type="image_generation",
            source_id=request_id,
            quantity=quantity,
        ) as usage:
            try:
                rendered = render(provider, options)
            except ImageProviderError as exc:
                usage.quantity = 0
                return _provider_failure(exc)
            except ImageRefError as exc:
                usage.quantity = 0
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            with transaction.atomic():
                generation = ImageGeneration.objects.create(
                    organization=organization,
                    owner=request.user,
                    mode=mode,
                    prompt=prompt,
                    negative_prompt=options["negative_prompt"],
                    model=options["model"],
                    provider=provider.name,
                    aspect_ratio=options["aspect_ratio"],
                    style=options["style"],
                    seed=options["seed"],
                    image_count=len(rendered),
                )
                for item in rendered:
                    _save_image(
                        item.content,
                        uuid.uuid4(),
                        organization=organization,
                        owner=request.user,
                        generation=generation,
                        mime_type=item.mime_type,
                        width=item.width,
                        height=item.height,
                    )
            usage.quantity = len(rendered)
        add_outbox_event(
            "images.generated" if mode == ImageGeneration.Mode.GENERATE else "images.edited",
            str(request_id),
            {"userId": str(request.user.id), "generationId": str(generation.id), "count": len(rendered)},
        )
        return Response(serialize_generation(generation), status=status.HTTP_201_CREATED)


@extend_schema(request=OpenApiTypes.OBJECT, responses={201: OpenApiTypes.OBJECT})
class ImageGenerateView(_ImageWriteView):
    def post(self, request: Request) -> Response:
        prompt = str(request.data.get("prompt") or "").strip()
        if not prompt:
            return Response({"detail": "prompt required"}, status=status.HTTP_400_BAD_REQUEST)

        def render(provider, options):  # type: ignore[no-untyped-def]
            return provider.generate(
                prompt,
                count=options["count"],
                aspect_ratio=options["aspect_ratio"],
                negative_prompt=options["negative_prompt"],
                seed=options["seed"],
            )

        return self._run(request, mode=ImageGeneration.Mode.GENERATE, prompt=prompt, render=render)


@extend_schema(request=OpenApiTypes.OBJECT, responses={201: OpenApiTypes.OBJECT})
class ImageEditJsonView(_ImageWriteView):
    """``{instruction, referenceImageRef, ...}`` - the source is a data URL, image id or file id."""

    def post(self, request: Request) -> Response:
        instruction = str(request.data.get("instruction") or request.data.get("prompt") or "").strip()
        reference = str(request.data.get("referenceImageRef") or request.data.get("image") or "")
        if not instruction or not reference:
            return Response(
                {"detail": "instruction and referenceImageRef are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        organization = organization_for_request(request, required=True)

        def render(provider, options):  # type: ignore[no-untyped-def]
            source, mime = load_image_ref(reference, request.user, organization)
            return [provider.edit(source, mime, instruction)]

        return self._run(request, mode=ImageGeneration.Mode.EDIT, prompt=instruction, render=render)


@extend_schema(responses={200: OpenApiTypes.OBJECT})
class ImageModelsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        return Response(available_models())


class ImageAssetSchema(serializers.Serializer):
    id = serializers.UUIDField()
    url = serializers.CharField()
    width = serializers.IntegerField()
    height = serializers.IntegerField()


class ImageGenerationSchema(serializers.Serializer):
    """Documentation shape of the frontend ``ImageGeneration`` (see ``serialize_generation``)."""

    id = serializers.UUIDField()
    prompt = serializers.CharField()
    negativePrompt = serializers.CharField(allow_null=True)
    model = serializers.CharField()
    aspectRatio = serializers.CharField()
    style = serializers.CharField()
    seed = serializers.IntegerField(allow_null=True)
    imageCount = serializers.IntegerField()
    createdAt = serializers.DateTimeField()
    images = ImageAssetSchema(many=True)
    favorite = serializers.BooleanField()
    mode = serializers.ChoiceField(choices=ImageGeneration.Mode.choices)
    answer = serializers.CharField(allow_null=True)


class ImageGenerationViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """The caller's image gallery (``GET/PATCH/DELETE /images/{id}/``)."""

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    lookup_value_regex = "[0-9a-f-]{36}"
    queryset = ImageGeneration.objects.none()
    serializer_class = ImageGenerationSchema

    def get_queryset(self):  # type: ignore[no-untyped-def]
        if getattr(self, "swagger_fake_view", False):
            return ImageGeneration.objects.none()
        organization = organization_for_request(self.request, required=True)
        return ImageGeneration.objects.filter(organization=organization, owner=self.request.user).exclude(
            mode=ImageGeneration.Mode.UNDERSTAND
        )

    @extend_schema(operation_id="v1_images_list", responses={200: ImageGenerationSchema(many=True)})
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return Response([serialize_generation(g) for g in self.get_queryset()[:200]])

    @extend_schema(responses={200: ImageGenerationSchema})
    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return Response(serialize_generation(self.get_object()))

    @extend_schema(request=OpenApiTypes.OBJECT, responses={200: OpenApiTypes.OBJECT})
    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        generation = self.get_object()
        if "favorite" in request.data:
            generation.favorite = bool(request.data["favorite"])
            generation.save(update_fields=["favorite"])
        return Response(serialize_generation(generation))

    def perform_destroy(self, instance: ImageGeneration) -> None:
        for image in instance.images.select_related("asset"):
            soft_delete_asset(image.asset)
        instance.delete()

    @extend_schema(request=None, responses={201: OpenApiTypes.OBJECT})
    @action(detail=True, methods=["post"], url_path="save-to-files")
    def save_to_files(self, request: Request, pk: Any = None) -> Response:
        """Generated images are already stored as files; return the first one's file record."""
        from apps.assets.views import _serialize

        generation = self.get_object()
        image = generation.images.select_related("asset").exclude(asset=None).order_by("created_at").first()
        if image is None:
            return Response(
                {"detail": "This image is not stored in file storage."}, status=status.HTTP_409_CONFLICT
            )
        return Response(_serialize([image.asset])[0], status=status.HTTP_201_CREATED)
