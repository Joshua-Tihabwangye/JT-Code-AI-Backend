from __future__ import annotations

import io
import uuid
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.assets.services import register_generated_asset, soft_delete_asset
from apps.assets.supabase_storage import generate_signed_delivery_url, supabase_storage_is_configured
from apps.core.pagination import ExportFormatMixin
from apps.core.throttling import BurstThrottle, ConversionThrottle
from apps.documents.models import Document, DocumentVersion
from apps.documents.rendering import render_docx, render_pdf
from apps.documents.serializers import (
    DocumentCreateSerializer,
    DocumentRenderSerializer,
    DocumentSerializer,
)
from apps.events.outbox import add_outbox_event
from apps.identity.authorization import (
    HasOrganizationWriteAccess,
    organization_for_request,
    tenant_scoped_queryset,
)

RENDER_ROOT = Path(settings.BASE_DIR) / "rendered_documents"


class DocumentViewSet(ExportFormatMixin, viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = DocumentSerializer
    lookup_field = "id"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return tenant_scoped_queryset(Document.objects.all(), self.request.user)

    def get_serializer_class(self):
        if self.action == "create":
            return DocumentCreateSerializer
        return DocumentSerializer

    def create(self, request: Request, *args, **kwargs):
        create_serializer = DocumentCreateSerializer(data=request.data)
        create_serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            document = create_serializer.save(
                owner=request.user,
                organization=organization_for_request(request, required=True),
            )
            DocumentVersion.objects.create(
                document=document, version=1, content=document.content, created_by=request.user
            )
        return Response(DocumentSerializer(document).data, status=status.HTTP_201_CREATED)

    def perform_create(self, serializer):
        serializer.save(
            owner=self.request.user,
            organization=organization_for_request(self.request, required=True),
        )

    def perform_update(self, serializer):
        """Edits (autosave) keep the version number; ``POST versions/`` snapshots a new
        version. Any content change invalidates a previously rendered file."""
        content_changed = "content" in serializer.validated_data and (
            serializer.validated_data["content"] != serializer.instance.content
        )
        instance = serializer.save()
        if not content_changed:
            return
        soft_delete_asset(instance.rendered_asset)
        instance.status = Document.Status.DRAFT
        instance.download_url = ""
        instance.rendered_asset = None
        instance.page_count = None
        instance.save(update_fields=["status", "download_url", "rendered_asset", "page_count", "updated_at"])

    @extend_schema(request=None, responses={201: DocumentSerializer})
    @action(detail=True, methods=["post"])
    def duplicate(self, request: Request, id=None):
        source = self.get_object()
        with transaction.atomic():
            copy = Document.objects.create(
                owner=request.user,
                organization=source.organization,
                title=f"{source.title} (copy)"[:500],
                template=source.template,
                template_version=source.template_version,
                content=source.content,
            )
            DocumentVersion.objects.create(
                document=copy, version=1, content=copy.content, created_by=request.user
            )
        return Response(DocumentSerializer(copy).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses={201: DocumentSerializer})
    @action(detail=True, methods=["post"])
    def versions(self, request: Request, id=None):
        """Snapshot the current content as the next version."""
        with transaction.atomic():
            document = Document.objects.select_for_update().get(pk=self.get_object().pk)
            document.version += 1
            document.save(update_fields=["version", "updated_at"])
            DocumentVersion.objects.create(
                document=document, version=document.version, content=document.content, created_by=request.user
            )
        return Response(DocumentSerializer(document).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        parameters=[OpenApiParameter("format", str, enum=["md", "pdf"], required=False)],
        responses={200: OpenApiTypes.OBJECT},
    )
    @action(detail=True, methods=["get"])
    def export(self, request: Request, id=None):
        document = self.get_object()
        export_format = request.query_params.get("format", "md")
        if export_format == "md":
            return Response({"content": f"# {document.title}\n\n{document.content}", "format": "md"})
        if export_format != "pdf":
            return Response({"detail": "format must be md or pdf."}, status=status.HTTP_400_BAD_REQUEST)
        response = HttpResponse(render_pdf(document), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{_safe_name(document.title)}.pdf"'
        return response

    @extend_schema(request=None, responses={201: OpenApiTypes.OBJECT})
    @action(detail=True, methods=["post"], url_path="save-to-files")
    def save_to_files(self, request: Request, id=None):
        """Store the document as a Markdown file in the caller's private asset storage."""
        from apps.assets.views import _serialize

        document = self.get_object()
        if not supabase_storage_is_configured():
            return Response(
                {"detail": "File storage is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        asset = register_generated_asset(
            f"# {document.title}\n\n{document.content}".encode(),
            owner=request.user,
            organization=document.organization,
            file_name=f"{_safe_name(document.title)}.md",
            kind="documents",
            content_type="text/markdown",
            provenance={"document_id": str(document.id), "document_version": document.version},
        )
        return Response(_serialize([asset])[0], status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        soft_delete_asset(instance.rendered_asset)
        if instance.download_url:
            self._remove_local_render(instance)
        instance.delete()

    def _remove_local_render(self, instance: Document) -> None:
        if RENDER_ROOT.exists():
            for path in RENDER_ROOT.glob(f"{instance.id}.*"):
                path.unlink(missing_ok=True)

    def _save_render(self, instance: Document, content: bytes, fmt: str):
        if supabase_storage_is_configured():
            content_types = {
                "pdf": "application/pdf",
                "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }
            asset = register_generated_asset(
                content,
                owner=instance.owner,
                organization=instance.organization,
                file_name=f"{instance.id}-v{instance.version}.{fmt}",
                kind="documents",
                content_type=content_types[fmt],
                provenance={"document_id": str(instance.id), "document_version": instance.version},
            )
            return generate_signed_delivery_url(asset.storage_key), asset
        if not settings.ASSET_LOCAL_FALLBACK_ENABLED:
            raise RuntimeError(
                "Supabase Storage is required for rendered documents in deployable environments."
            )
        RENDER_ROOT.mkdir(parents=True, exist_ok=True)
        path = RENDER_ROOT / f"{instance.id}.{fmt}"
        with open(path, "wb") as fh:
            fh.write(content)
        from django.urls import reverse

        return f"{reverse('document-download', kwargs={'id': instance.id})}?fmt={fmt}", None

    @action(detail=True, methods=["post"], throttle_classes=[ConversionThrottle, BurstThrottle])
    def render(self, request: Request, id=None):
        document = self.get_object()
        serializer = DocumentRenderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        fmt = serializer.validated_data["format"]
        requested_version = serializer.validated_data.get("version")
        if requested_version and requested_version != document.version:
            return Response(
                {"detail": "Document version conflict; refresh and retry."},
                status=status.HTTP_409_CONFLICT,
            )

        from apps.usage import services as metering
        from apps.usage.models import Feature

        reservation = metering.reserve(
            organization=document.organization,
            user=request.user,
            feature=Feature.DOCUMENT_RENDERS,
            source_type="document_render",
            source_id=uuid.uuid4(),
        )
        document.status = Document.Status.RENDERING
        document.error_message = ""
        document.save(update_fields=["status", "error_message", "updated_at"])

        try:
            if fmt == "pdf":
                content = render_pdf(document)
                from pypdf import PdfReader

                pages = len(PdfReader(io.BytesIO(content)).pages)
            else:
                content = render_docx(document)
                pages = None
        except Exception as exc:
            metering.release(reservation.id, reason="render failed")
            document.status = Document.Status.FAILED
            document.error_message = str(exc)[:500]
            document.save(update_fields=["status", "error_message", "updated_at"])
            add_outbox_event(
                "document.render.failed",
                str(document.id),
                {
                    "documentId": str(document.id),
                    "userId": str(document.owner_id),
                    "error": document.error_message,
                },
            )
            return Response(
                {"detail": "Document rendering failed."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        try:
            download_url, rendered_asset = self._save_render(document, content, fmt)
        except Exception as exc:
            metering.release(reservation.id, reason="render storage failed")
            document.status = Document.Status.FAILED
            document.error_message = str(exc)[:500]
            document.save(update_fields=["status", "error_message", "updated_at"])
            return Response(
                {"detail": "Rendered asset storage failed."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        metering.settle(reservation.id)
        soft_delete_asset(document.rendered_asset)
        document.status = Document.Status.READY
        document.download_url = download_url if rendered_asset is None else ""
        document.rendered_asset = rendered_asset
        document.page_count = pages
        document.save(update_fields=["status", "download_url", "rendered_asset", "page_count", "updated_at"])

        add_outbox_event(
            "document.render.completed",
            str(document.id),
            {
                "documentId": str(document.id),
                "userId": str(document.owner_id),
                "format": fmt,
                "pages": pages,
                "downloadUrl": download_url,
            },
        )

        return Response(
            {
                "id": str(document.id),
                "status": document.status,
                "format": fmt,
                "pages": pages,
                "download_url": download_url,
                "asset_id": str(rendered_asset.id) if rendered_asset else None,
            }
        )


def _safe_name(title: str) -> str:
    return "".join(ch if ch.isalnum() or ch in " -_." else "_" for ch in title).strip()[:120] or "document"


@extend_schema(responses={200: OpenApiTypes.BINARY})
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def document_download(request: Request, id: uuid.UUID) -> FileResponse:
    """Serves locally rendered documents when Supabase Storage is not configured."""
    fmt = request.GET.get("fmt", "pdf")
    if fmt not in {"pdf", "docx"}:
        raise Http404
    document = tenant_scoped_queryset(
        Document.objects.filter(id=id),
        request.user,
    ).first()
    if not document or not document.download_url:
        raise Http404
    path = RENDER_ROOT / f"{id}.{fmt}"
    return FileResponse(open(path, "rb"), as_attachment=True, filename=f"{document.title}.{fmt}")
