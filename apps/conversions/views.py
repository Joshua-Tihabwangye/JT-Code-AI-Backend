from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

from django.http import FileResponse, Http404
from rest_framework import status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.billing.services import CreditService
from apps.conversions.converter import CONVERSION_ROOT, finalize_conversion, run_conversion
from apps.conversions.models import ConversionJob
from apps.conversions.serializers import (
    ALLOWED_MATRIX,
    MAX_CONVERSION_INPUT_BYTES,
    ConversionCreateSerializer,
    ConversionJobSerializer,
)
from apps.events.outbox import add_outbox_event


class ConversionViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ConversionJobSerializer
    lookup_field = 'id'
    http_method_names = ['get', 'post', 'head', 'options']

    def get_queryset(self):
        return ConversionJob.objects.filter(owner=self.request.user)

    def create(self, request: Request) -> Response:
        serializer = ConversionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        input_format = serializer.validated_data['input_format'].lower()
        output_format = serializer.validated_data['output_format'].lower()
        if (input_format, output_format) not in ALLOWED_MATRIX:
            return Response(
                {'detail': f'Conversion {input_format}->{output_format} is not allowed.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        uploaded = serializer.validated_data.get('file')
        content = serializer.validated_data.get('content', '')
        if uploaded is None and not content.strip():
            return Response(
                {'detail': 'Either file or content is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if uploaded is not None:
            if uploaded.size > MAX_CONVERSION_INPUT_BYTES:
                return Response(
                    {'detail': 'File exceeds the conversion size limit.'},
                    status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                )
            content = uploaded.read()
            input_bytes = uploaded.size
            input_filename = uploaded.name or f'input.{input_format}'
        else:
            content = content.encode('utf-8')
            input_bytes = len(content)
            input_filename = f'input.{input_format}'

        estimated_credits = Decimal('10') if output_format != 'pdf' else Decimal('20')
        try:
            CreditService.reserve_credits(
                user=request.user,
                amount=estimated_credits,
                request_id=uuid.uuid4(),
                reason=f'File conversion {input_format}->{output_format}',
            )
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)

        job = ConversionJob.objects.create(
            owner=request.user,
            organization=request.user.organizations.first(),
            input_filename=input_filename,
            input_format=input_format,
            output_format=output_format,
            input_bytes=input_bytes,
            reserved_credits=estimated_credits,
            options={
                'input_format': input_format,
                'output_format': output_format,
            },
        )

        CONVERSION_ROOT.mkdir(parents=True, exist_ok=True)
        if uploaded is not None:
            input_path = CONVERSION_ROOT / f'{job.id}.input.{input_format}'
            input_path.write_bytes(content)
            job.input_path = str(input_path)
            job.save(update_fields=['input_path'])

        add_outbox_event('conversion.job.created', str(job.id), {
            'conversionId': str(job.id),
            'userId': str(request.user.id),
            'inputFormat': input_format,
            'outputFormat': output_format,
            'inputBytes': input_bytes,
        })

        try:
            job.status = ConversionJob.Status.RUNNING
            job.save(update_fields=['status', 'updated_at'])
            output = run_conversion(job)
            cloudinary_url = finalize_conversion(job, output)
            job.status = ConversionJob.Status.COMPLETED
            job.save(update_fields=['status', 'output_bytes', 'output_path', 'output_url', 'updated_at'])
            add_outbox_event('conversion.job.completed', str(job.id), {
                'conversionId': str(job.id),
                'userId': str(request.user.id),
                'outputBytes': job.output_bytes,
                'outputUrl': cloudinary_url,
            })
        except Exception as exc:
            job.status = ConversionJob.Status.FAILED
            job.error_message = str(exc)[:500]
            job.save(update_fields=['status', 'error_message', 'updated_at'])
            add_outbox_event('conversion.job.failed', str(job.id), {
                'conversionId': str(job.id),
                'userId': str(request.user.id),
                'error': job.error_message,
            })
            return Response(
                {'detail': 'Conversion failed.', 'error': job.error_message},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        return Response(ConversionJobSerializer(job).data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def conversion_download(request: Request, id: uuid.UUID) -> FileResponse:
    job = ConversionJob.objects.filter(id=id, owner=request.user).first()
    if not job or job.status != ConversionJob.Status.COMPLETED or not job.output_path:
        raise Http404
    path = Path(job.output_path)
    if not path.exists():
        raise Http404
    return FileResponse(
        open(path, 'rb'),
        as_attachment=True,
        filename=f'{Path(job.input_filename).stem}.{job.output_format}',
    )
