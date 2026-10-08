from __future__ import annotations

import uuid
from pathlib import Path

from django.http import FileResponse, Http404
from drf_spectacular.utils import OpenApiTypes, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.conversions.converter import CONVERSION_ROOT, finalize_conversion, run_conversion
from apps.conversions.models import ConversionJob
from apps.conversions.serializers import (
    ALLOWED_MATRIX,
    MAX_CONVERSION_INPUT_BYTES,
    ConversionCreateSerializer,
    ConversionJobSerializer,
)
from apps.events.outbox import add_outbox_event
from apps.identity.authorization import (
    HasOrganizationWriteAccess,
    organization_for_request,
    tenant_scoped_queryset,
)
from apps.usage import services as metering
from apps.usage.models import Feature
from apps.usage.pricing import flat_credits


class ConversionViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = ConversionJobSerializer
    lookup_field = "id"
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        return tenant_scoped_queryset(ConversionJob.objects.all(), self.request.user)

    def create(self, request: Request) -> Response:
        serializer = ConversionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        input_format = serializer.validated_data["input_format"].lower()
        output_format = serializer.validated_data["output_format"].lower()
        if (input_format, output_format) not in ALLOWED_MATRIX:
            return Response(
                {"detail": f"Conversion {input_format}->{output_format} is not allowed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        uploaded = serializer.validated_data.get("file")
        content = serializer.validated_data.get("content", "")
        if uploaded is None and not content.strip():
            return Response(
                {"detail": "Either file or content is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if uploaded is not None:
            if uploaded.size > MAX_CONVERSION_INPUT_BYTES:
                return Response(
                    {"detail": "File exceeds the conversion size limit."},
                    status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                )
            content = uploaded.read()
            input_bytes = uploaded.size
            input_filename = uploaded.name or f"input.{input_format}"
        else:
            content = content.encode("utf-8")
            input_bytes = len(content)
            input_filename = f"input.{input_format}"

        organization = organization_for_request(request, required=True)
        # PDF output needs a rendering pass, so it is priced at twice the flat rate.
        price = flat_credits(Feature.FILE_CONVERSIONS) * (2 if output_format == "pdf" else 1)
        job_id = uuid.uuid4()
        reservation = metering.reserve(
            organization=organization,
            user=request.user,
            feature=Feature.FILE_CONVERSIONS,
            source_type="conversion",
            source_id=job_id,
            credits=price,
        )

        job = ConversionJob.objects.create(
            id=job_id,
            owner=request.user,
            organization=organization,
            input_filename=input_filename,
            input_format=input_format,
            output_format=output_format,
            input_bytes=input_bytes,
            reserved_credits=reservation.credits_reserved,
            options={
                "input_format": input_format,
                "output_format": output_format,
            },
        )

        CONVERSION_ROOT.mkdir(parents=True, exist_ok=True)
        if uploaded is not None:
            input_path = CONVERSION_ROOT / f"{job.id}.input.{input_format}"
            input_path.write_bytes(content)
            job.input_path = str(input_path)
            job.save(update_fields=["input_path"])

        add_outbox_event(
            "conversion.job.created",
            str(job.id),
            {
                "conversionId": str(job.id),
                "userId": str(request.user.id),
                "inputFormat": input_format,
                "outputFormat": output_format,
                "inputBytes": input_bytes,
            },
        )

        try:
            job.status = ConversionJob.Status.RUNNING
            job.save(update_fields=["status", "updated_at"])
            output = run_conversion(job)
            storage_url = finalize_conversion(job, output)
            job.status = ConversionJob.Status.COMPLETED
            job.save(
                update_fields=[
                    "status",
                    "output_bytes",
                    "output_path",
                    "output_url",
                    "output_asset",
                    "updated_at",
                ]
            )
            add_outbox_event(
                "conversion.job.completed",
                str(job.id),
                {
                    "conversionId": str(job.id),
                    "userId": str(request.user.id),
                    "outputBytes": job.output_bytes,
                    "outputUrl": storage_url,
                },
            )
        except Exception as exc:
            metering.release(reservation.id, reason="conversion failed")
            job.status = ConversionJob.Status.FAILED
            job.error_message = str(exc)[:500]
            job.save(update_fields=["status", "error_message", "updated_at"])
            add_outbox_event(
                "conversion.job.failed",
                str(job.id),
                {
                    "conversionId": str(job.id),
                    "userId": str(request.user.id),
                    "error": job.error_message,
                },
            )
            return Response(
                {"detail": "Conversion failed.", "error": job.error_message},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        metering.settle(reservation.id, credits=price)
        return Response(ConversionJobSerializer(job).data, status=status.HTTP_201_CREATED)


@extend_schema(responses={200: OpenApiTypes.BINARY})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def conversion_download(request: Request, id: uuid.UUID) -> FileResponse:
    job = tenant_scoped_queryset(
        ConversionJob.objects.filter(id=id),
        request.user,
    ).first()
    if not job or job.status != ConversionJob.Status.COMPLETED or not job.output_path:
        raise Http404
    path = Path(job.output_path)
    if not path.exists():
        raise Http404
    return FileResponse(
        open(path, "rb"),
        as_attachment=True,
        filename=f"{Path(job.input_filename).stem}.{job.output_format}",
    )
