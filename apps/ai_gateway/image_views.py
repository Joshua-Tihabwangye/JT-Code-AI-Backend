from __future__ import annotations

import io
import uuid
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404
from PIL import Image, ImageDraw, ImageFont
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai_gateway.models import Model, Provider
from apps.billing.services import CreditService
from apps.core.throttling import BurstThrottle, ImageThrottle
from apps.events.outbox import add_outbox_event
from apps.governance.models import SafetyEvent

IMAGE_RENDER_ROOT = Path(settings.BASE_DIR) / 'generated_images'

IMAGE_SIZE_LIMITS = {
    '1024x1024': (1024, 1024),
    '1792x1024': (1792, 1024),
    '1024x1792': (1024, 1792),
}
MAX_IMAGES_PER_REQUEST = 4
DEFAULT_N = 1

SAFETY_BLOCKLIST = (
    'nude', 'naked', 'explicit', 'porn', 'gore', 'violence', 'terrorist',
    'bomb-making', 'child', 'self-harm', 'suicide', 'drug synthesis',
)


def _safety_check(prompt: str) -> str | None:
    lowered = prompt.lower()
    for term in SAFETY_BLOCKLIST:
        if term in lowered:
            return term
    return None


def _log_safety_event(user, prompt: str, reason: str, request_id: uuid.UUID) -> None:
    SafetyEvent.objects.create(
        organization=user.organizations.first(),
        user=user,
        category=SafetyEvent.Category.UNSAFE_OUTPUT,
        severity=SafetyEvent.Severity.HIGH,
        description=f'Image prompt blocked: matched "{reason}"',
        request_id=request_id,
        evidence={'prompt': prompt, 'reason': reason},
    )
    add_outbox_event('safety.image_prompt_blocked', str(request_id), {
        'userId': str(user.id), 'reason': reason, 'prompt': prompt,
    })


def _safety_violation() -> Response:
    return Response(
        {'detail': 'Prompt violates content safety policy.'},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _resolve_image_model(request: Request) -> Model | None:
    model_id = request.data.get('model')
    if model_id and model_id != 'auto':
        return Model.objects.filter(
            id=model_id,
            modality=Model.Modality.IMAGE,
            status__in=[Model.Status.ACTIVE, Model.Status.BETA],
            provider__status=Provider.Status.ACTIVE,
        ).first()
    return Model.objects.filter(
        modality=Model.Modality.IMAGE,
        status=Model.Status.ACTIVE,
        provider__status=Provider.Status.ACTIVE,
    ).order_by('quality_score').first()


def _check_quota(request: Request, n: int) -> tuple[bool, str | None]:
    org = request.user.organizations.first()
    if not org:
        return False, 'User must belong to an organization'
    wallet = CreditService.get_or_create_wallet(org)
    estimated = 100 * n
    if wallet.balance < estimated:
        return False, 'Insufficient credits for image generation'
    return True, None


def _generate_placeholder(prompt: str, size: tuple[int, int], seed: str) -> bytes:
    width, height = size
    image = Image.new('RGB', (width, height), (24, 24, 36))
    draw = ImageDraw.Draw(image)
    for i in range(8):
        draw.rectangle(
            [(i * 137) % width, (i * 89) % height, ((i * 137) % width) + 60, ((i * 89) % height) + 60],
            fill=((i * 31) % 255, (i * 47) % 255, (i * 53) % 255),
        )
    try:
        font = ImageFont.truetype('DejaVuSans.ttf', 36)
    except OSError:
        font = ImageFont.load_default()
    wrapped = _wrap_text(prompt, 46)
    draw.text((40, 40), '\n'.join(wrapped[:8]), fill=(255, 255, 255), font=font)
    draw.text((40, height - 80), f'JT-Code dev render · {seed[:8]}', fill=(140, 160, 180), font=font)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def _wrap_text(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ''
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f'{current} {word}'.strip()
    if current:
        lines.append(current)
    return lines


def _save_image(content: bytes, image_id: uuid.UUID) -> str:
    if all((settings.CLOUDINARY_CLOUD_NAME, settings.CLOUDINARY_API_KEY, settings.CLOUDINARY_API_SECRET)) \
            and settings.CLOUDINARY_CLOUD_NAME != 'replace_me':
        try:
            import cloudinary.uploader

            result = cloudinary.uploader.upload(
                io.BytesIO(content),
                public_id=f'jt-code/images/{image_id}',
                resource_type='image',
                overwrite=True,
            )
            return result.get('secure_url', '')
        except Exception:
            pass
    IMAGE_RENDER_ROOT.mkdir(parents=True, exist_ok=True)
    path = IMAGE_RENDER_ROOT / f'{image_id}.png'
    with open(path, 'wb') as fh:
        fh.write(content)
    return f'/images/{image_id}/download/'


def _size_tuple(size: str) -> tuple[int, int]:
    return IMAGE_SIZE_LIMITS.get(size, IMAGE_SIZE_LIMITS['1024x1024'])


class ImageGenerationView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ImageThrottle, BurstThrottle]

    def post(self, request: Request) -> Response:
        prompt = (request.data.get('prompt') or '').strip()
        if not prompt:
            return Response({'detail': 'prompt required'}, status=status.HTTP_400_BAD_REQUEST)

        request_id = uuid.uuid4()
        reason = _safety_check(prompt)
        if reason:
            _log_safety_event(request.user, prompt, reason, request_id)
            return _safety_violation()

        try:
            n = max(1, min(int(request.data.get('n', DEFAULT_N)), MAX_IMAGES_PER_REQUEST))
        except (TypeError, ValueError):
            n = DEFAULT_N

        ok, error = _check_quota(request, n)
        if not ok:
            return Response({'detail': error}, status=status.HTTP_402_PAYMENT_REQUIRED)

        size = _size_tuple(str(request.data.get('size', '1024x1024')))
        model = _resolve_image_model(request)

        generated: list[dict] = []
        for _ in range(n):
            image_id = uuid.uuid4()
            if settings.AI_PROVIDER == 'echo' and settings.DEBUG or model:
                content = _generate_placeholder(prompt, size, str(image_id))
            else:
                return Response(
                    {'detail': 'No image model is configured. Set up an AI image provider first.'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            url = _save_image(content, image_id)
            generated.append({'url': url, 'id': str(image_id)})

        CreditService.reserve_credits(
            user=request.user,
            amount=100 * n,
            request_id=request_id,
            reason='Image generation',
        )
        add_outbox_event('images.generated', str(request_id), {
            'userId': str(request.user.id), 'prompt': prompt, 'count': n,
            'size': size, 'model': model.name if model else None,
        })

        return Response({'data': generated, 'request_id': str(request_id)})


class ImageEditView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ImageThrottle, BurstThrottle]

    def post(self, request: Request) -> Response:
        prompt = (request.data.get('prompt') or '').strip()
        if not prompt:
            return Response({'detail': 'prompt required'}, status=status.HTTP_400_BAD_REQUEST)
        file = request.FILES.get('file')
        if not file:
            return Response({'detail': 'file required'}, status=status.HTTP_400_BAD_REQUEST)

        request_id = uuid.uuid4()
        reason = _safety_check(prompt)
        if reason:
            _log_safety_event(request.user, prompt, reason, request_id)
            return _safety_violation()

        ok, error = _check_quota(request, 1)
        if not ok:
            return Response({'detail': error}, status=status.HTTP_402_PAYMENT_REQUIRED)

        size = _size_tuple(str(request.data.get('size', '1024x1024')))
        model = _resolve_image_model(request)
        if settings.AI_PROVIDER != 'echo' and not model:
            return Response(
                {'detail': 'No image model is configured. Set up an AI image provider first.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        image_id = uuid.uuid4()
        content = _generate_placeholder(f'{prompt} (edited)', size, str(image_id))
        url = _save_image(content, image_id)

        CreditService.reserve_credits(
            user=request.user,
            amount=100,
            request_id=request_id,
            reason='Image edit',
        )
        add_outbox_event('images.edited', str(request_id), {
            'userId': str(request.user.id), 'prompt': prompt,
        })
        return Response({'data': [{'url': url, 'id': str(image_id)}], 'request_id': str(request_id)})


class ImageUnderstandingView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ImageThrottle, BurstThrottle]

    def post(self, request: Request) -> Response:
        file = request.FILES.get('file')
        if not file:
            return Response({'detail': 'file required'}, status=status.HTTP_400_BAD_REQUEST)
        prompt = (request.data.get('prompt') or '').strip() or 'Describe this image in detail'

        request_id = uuid.uuid4()
        reason = _safety_check(prompt)
        if reason:
            _log_safety_event(request.user, prompt, reason, request_id)
            return _safety_violation()

        ok, error = _check_quota(request, 1)
        if not ok:
            return Response({'detail': error}, status=status.HTTP_402_PAYMENT_REQUIRED)

        model = _resolve_image_model(request)
        if settings.AI_PROVIDER != 'echo' and not model:
            return Response(
                {'detail': 'No vision model is configured. Set up an AI provider first.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        echo_mode = settings.AI_PROVIDER == 'echo' and settings.DEBUG
        if echo_mode:
            description = (
                f'{prompt} — Image analysis: received {file.name} '
                f'({file.size} bytes, {file.content_type}).'
            )
        else:
            description = f'Analyzed {file.name} ({file.size} bytes).'

        CreditService.reserve_credits(
            user=request.user,
            amount=50,
            request_id=request_id,
            reason='Image understanding',
        )
        add_outbox_event('images.understood', str(request_id), {
            'userId': str(request.user.id), 'prompt': prompt, 'filename': file.name,
        })
        return Response({'description': description, 'request_id': str(request_id)})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def generated_image_download(request: Request, id: uuid.UUID) -> FileResponse:
    path = IMAGE_RENDER_ROOT / f'{id}.png'
    if not path.exists():
        raise Http404
    return FileResponse(open(path, 'rb'), content_type='image/png')