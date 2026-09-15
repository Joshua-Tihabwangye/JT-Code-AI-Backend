from __future__ import annotations

import io
import uuid
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.core.throttling import BurstThrottle, ConversionThrottle
from apps.documents.models import Document
from apps.documents.rendering import render_docx, render_pdf, upload_bytes_to_cloudinary
from apps.documents.serializers import (
    DocumentCreateSerializer,
    DocumentRenderSerializer,
    DocumentSerializer,
)
from apps.events.outbox import add_outbox_event

RENDER_ROOT = Path(settings.BASE_DIR) / 'rendered_documents'


class DocumentViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = DocumentSerializer
    lookup_field = 'id'
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        return Document.objects.filter(owner=self.request.user)

    def get_serializer_class(self):
        if self.action == 'create':
            return DocumentCreateSerializer
        return DocumentSerializer

    def create(self, request: Request, *args, **kwargs):
        create_serializer = DocumentCreateSerializer(data=request.data)
        create_serializer.is_valid(raise_exception=True)
        document = create_serializer.save(
            owner=request.user,
            organization=request.user.organizations.first(),
        )
        return Response(DocumentSerializer(document).data, status=status.HTTP_201_CREATED)

    def perform_create(self, serializer):
        serializer.save(
            owner=self.request.user,
            organization=self.request.user.organizations.first(),
        )

    def perform_update(self, serializer):
        instance = serializer.save()
        instance.version += 1
        instance.status = Document.Status.DRAFT
        instance.download_url = ''
        instance.page_count = None
        instance.save(update_fields=['version', 'status', 'download_url', 'page_count', 'updated_at'])

    def perform_destroy(self, instance):
        if instance.download_url:
            self._remove_local_render(instance)
        instance.delete()

    def _remove_local_render(self, instance: Document) -> None:
        if RENDER_ROOT.exists():
            for path in RENDER_ROOT.glob(f'{instance.id}.*'):
                path.unlink(missing_ok=True)

    def _save_render(self, instance: Document, content: bytes, fmt: str) -> str:
        url = upload_bytes_to_cloudinary(content, f'jt-code/documents/{instance.id}', resource_type='raw')
        if url:
            return url
        RENDER_ROOT.mkdir(parents=True, exist_ok=True)
        path = RENDER_ROOT / f'{instance.id}.{fmt}'
        with open(path, 'wb') as fh:
            fh.write(content)
        from django.urls import reverse

        return f"{reverse('document-download', kwargs={'id': instance.id})}?fmt={fmt}"

    @action(detail=True, methods=['post'], throttle_classes=[ConversionThrottle, BurstThrottle])
    def render(self, request: Request, id=None):
        document = self.get_object()
        serializer = DocumentRenderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        fmt = serializer.validated_data['format']
        requested_version = serializer.validated_data.get('version')
        if requested_version and requested_version != document.version:
            return Response(
                {'detail': 'Document version conflict; refresh and retry.'},
                status=status.HTTP_409_CONFLICT,
            )

        document.status = Document.Status.RENDERING
        document.error_message = ''
        document.save(update_fields=['status', 'error_message', 'updated_at'])

        try:
            if fmt == 'pdf':
                content = render_pdf(document)
                from pypdf import PdfReader

                pages = len(PdfReader(io.BytesIO(content)).pages)
            else:
                content = render_docx(document)
                pages = None
        except Exception as exc:
            document.status = Document.Status.FAILED
            document.error_message = str(exc)[:500]
            document.save(update_fields=['status', 'error_message', 'updated_at'])
            add_outbox_event('document.render.failed', str(document.id), {
                'documentId': str(document.id), 'userId': str(document.owner_id),
                'error': document.error_message,
            })
            return Response(
                {'detail': 'Document rendering failed.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        download_url = self._save_render(document, content, fmt)
        document.status = Document.Status.READY
        document.download_url = download_url
        document.page_count = pages
        document.save(update_fields=['status', 'download_url', 'page_count', 'updated_at'])

        add_outbox_event('document.render.completed', str(document.id), {
            'documentId': str(document.id), 'userId': str(document.owner_id),
            'format': fmt, 'pages': pages, 'downloadUrl': download_url,
        })

        return Response({
            'id': str(document.id),
            'status': document.status,
            'format': fmt,
            'pages': pages,
            'download_url': download_url,
        })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def document_download(request: Request, id: uuid.UUID) -> FileResponse:
    """Serves locally rendered documents when Cloudinary is not configured."""
    fmt = request.GET.get('fmt', 'pdf')
    if fmt not in {'pdf', 'docx'}:
        raise Http404
    document = Document.objects.filter(id=id, owner=request.user).first()
    if not document or not document.download_url:
        raise Http404
    path = RENDER_ROOT / f'{id}.{fmt}'
    return FileResponse(open(path, 'rb'), as_attachment=True, filename=f'{document.title}.{fmt}')