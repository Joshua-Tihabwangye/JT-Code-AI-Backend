from __future__ import annotations

import hashlib
import json
import time

from django.db import IntegrityError, close_old_connections, transaction
from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.conversations.models import ChatRequest, Conversation, ConversationFeedback, Message
from apps.conversations.serializers import (
    ChatRequestCreateSerializer,
    ChatRequestListQuerySerializer,
    ChatRequestSerializer,
    ConversationFeedbackCreateSerializer,
    ConversationFeedbackSerializer,
    ConversationMessageCreateSerializer,
    ConversationSerializer,
    MessageSerializer,
)
from apps.conversations.tasks import process_chat_request
from apps.core.exceptions import IdempotencyConflict
from apps.core.pagination import CreatedCursorPagination, UpdatedCursorPagination
from apps.core.throttling import BurstThrottle, ChatThrottle
from apps.events.outbox import add_outbox_event
from apps.identity.authorization import (
    HasOrganizationWriteAccess,
    organization_for_request,
    tenant_scoped_queryset,
)


def _idempotency_key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key:
        raise ValidationError({"idempotencyKey": ["The Idempotency-Key header is required."]})
    if len(key) > 255:
        raise ValidationError({"idempotencyKey": ["The Idempotency-Key header is too long."]})
    return key


def _request_fingerprint(conversation: Conversation, content: str, timezone_name: str, locale: str) -> str:
    payload = json.dumps(
        {
            "conversationId": str(conversation.id),
            "content": content,
            "timezone": timezone_name,
            "locale": locale,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def create_chat_request(
    *,
    request: Request,
    conversation: Conversation,
    content: str,
    timezone_name: str = "",
    locale: str = "",
) -> tuple[ChatRequest, bool]:
    """Create a canonical chat request, or safely replay its previous result."""
    idempotency_key = _idempotency_key(request)
    fingerprint = _request_fingerprint(conversation, content, timezone_name, locale)
    try:
        with transaction.atomic():
            chat_request = ChatRequest.objects.create(
                owner=request.user,
                organization=conversation.organization,
                conversation=conversation,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                input_text=content,
                timezone=timezone_name,
                locale=locale,
                trace_id=getattr(request, "trace_id", ""),
            )
            Message.objects.create(
                conversation=conversation,
                organization=conversation.organization,
                role=Message.Role.USER,
                content=content,
            )
            add_outbox_event(
                "chat.request.accepted",
                str(chat_request.id),
                {
                    "requestId": str(chat_request.id),
                    "conversationId": str(conversation.id),
                    "userId": str(request.user.id),
                    "traceId": chat_request.trace_id,
                },
                headers={"request_id": getattr(request, "request_id", "")},
            )
            transaction.on_commit(lambda: process_chat_request.delay(str(chat_request.id)))
    except IntegrityError:
        chat_request = ChatRequest.objects.get(
            organization=conversation.organization,
            owner=request.user,
            idempotency_key=idempotency_key,
        )
        if chat_request.request_fingerprint != fingerprint:
            raise IdempotencyConflict from None
        return chat_request, True
    return chat_request, False


class ConversationRuntimeViewSet(viewsets.ModelViewSet):
    """Version 1 conversation API with cursor reads and tenant-scoped writes."""

    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = ConversationSerializer
    pagination_class = UpdatedCursorPagination
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        queryset = tenant_scoped_queryset(Conversation.objects.all(), self.request.user)
        include_archived = self.request.query_params.get("includeArchived", "").lower() == "true"
        if self.action == "unarchive" or not include_archived:
            queryset = queryset.filter(archived_at__isnull=True)
        search = self.request.query_params.get("q", "").strip()
        if search:
            queryset = queryset.filter(title__icontains=search[:255])
        return queryset.order_by("-updated_at", "-id")

    def perform_create(self, serializer):
        serializer.save(
            owner=self.request.user,
            organization=organization_for_request(self.request, required=True),
        )

    @action(detail=True, methods=["post"])
    def archive(self, request: Request, pk=None) -> Response:
        conversation = self.get_object()
        if conversation.archived_at is None:
            conversation.archived_at = timezone.now()
            conversation.save(update_fields=("archived_at", "updated_at"))
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"])
    def unarchive(self, request: Request, pk=None) -> Response:
        conversation = self.get_object()
        if conversation.archived_at is not None:
            conversation.archived_at = None
            conversation.save(update_fields=("archived_at", "updated_at"))
        return Response(self.get_serializer(conversation).data)

    @action(
        detail=True,
        methods=["get", "post"],
        pagination_class=CreatedCursorPagination,
        throttle_classes=[ChatThrottle, BurstThrottle],
    )
    def messages(self, request: Request, pk=None) -> Response:
        conversation = self.get_object()
        if request.method == "GET":
            queryset = conversation.messages.filter(organization=conversation.organization).order_by(
                "-created_at", "-id"
            )
            page = self.paginate_queryset(queryset)
            if page is not None:
                return self.get_paginated_response(MessageSerializer(page, many=True).data)
            return Response(MessageSerializer(queryset, many=True).data)

        serializer = ConversationMessageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        chat_request, replayed = create_chat_request(
            request=request,
            conversation=conversation,
            content=serializer.validated_data["content"],
            timezone_name=serializer.validated_data.get("timezone", ""),
            locale=serializer.validated_data.get("locale", ""),
        )
        response = Response(
            ChatRequestSerializer(chat_request).data,
            status=status.HTTP_200_OK if replayed else status.HTTP_202_ACCEPTED,
        )
        if replayed:
            response["Idempotency-Replayed"] = "true"
        return response

    @action(
        detail=True,
        methods=["get", "post"],
        pagination_class=CreatedCursorPagination,
    )
    def feedback(self, request: Request, pk=None) -> Response:
        conversation = self.get_object()
        queryset = ConversationFeedback.objects.filter(
            conversation=conversation,
            organization=conversation.organization,
            owner=request.user,
        ).order_by("-created_at", "-id")
        if request.method == "GET":
            page = self.paginate_queryset(queryset)
            if page is not None:
                return self.get_paginated_response(ConversationFeedbackSerializer(page, many=True).data)
            return Response(ConversationFeedbackSerializer(queryset, many=True).data)

        serializer = ConversationFeedbackCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        chat_request = None
        chat_request_id = serializer.validated_data.get("chatRequestId")
        if chat_request_id:
            chat_request = ChatRequest.objects.filter(
                id=chat_request_id,
                conversation=conversation,
                organization=conversation.organization,
            ).first()
            if chat_request is None:
                raise NotFound("Chat request not found in this conversation.")
        defaults = {
            "rating": serializer.validated_data["rating"],
            "comment": serializer.validated_data.get("comment", ""),
            "metadata": serializer.validated_data.get("metadata", {}),
            "organization": conversation.organization,
            "conversation": conversation,
        }
        if chat_request is not None:
            feedback, created = ConversationFeedback.objects.update_or_create(
                owner=request.user,
                chat_request=chat_request,
                defaults=defaults,
            )
        else:
            feedback = ConversationFeedback.objects.create(
                owner=request.user,
                chat_request=None,
                **defaults,
            )
            created = True
        return Response(
            ConversationFeedbackSerializer(feedback).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ChatRequestRuntimeViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated, HasOrganizationWriteAccess]
    serializer_class = ChatRequestSerializer
    pagination_class = CreatedCursorPagination
    throttle_classes = [ChatThrottle, BurstThrottle]

    def get_queryset(self):
        return tenant_scoped_queryset(ChatRequest.objects.all(), self.request.user).select_related(
            "conversation"
        )

    def list(self, request: Request, *args, **kwargs) -> Response:
        filters = ChatRequestListQuerySerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        queryset = self.get_queryset().order_by("-created_at", "-id")
        if conversation_id := filters.validated_data.get("conversationId"):
            queryset = queryset.filter(conversation_id=conversation_id)
        if request_status := filters.validated_data.get("status"):
            queryset = queryset.filter(status=request_status)
        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(queryset, many=True).data)

    def create(self, request: Request, *args, **kwargs) -> Response:
        serializer = ChatRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conversation = tenant_scoped_queryset(
            Conversation.objects.filter(
                id=serializer.validated_data["conversationId"], archived_at__isnull=True
            ),
            request.user,
        ).first()
        if conversation is None:
            raise NotFound("Conversation not found.")
        chat_request, replayed = create_chat_request(
            request=request,
            conversation=conversation,
            content=serializer.validated_data["chatInput"],
            timezone_name=serializer.validated_data.get("timezone", ""),
            locale=serializer.validated_data.get("locale", ""),
        )
        response = Response(
            self.get_serializer(chat_request).data,
            status=status.HTTP_200_OK if replayed else status.HTTP_202_ACCEPTED,
        )
        if replayed:
            response["Idempotency-Replayed"] = "true"
        return response

    @action(detail=True, methods=["get"])
    def stream(self, request: Request, pk=None) -> StreamingHttpResponse:
        item = self.get_object()
        request_id = str(item.id)

        def event_stream():
            last_status = None
            started = time.monotonic()
            while time.monotonic() - started < 90:
                close_old_connections()
                current = tenant_scoped_queryset(
                    ChatRequest.objects.filter(id=request_id), request.user
                ).first()
                if current is None:
                    yield 'event: failed\ndata: {"code":"not_found","message":"Request not found."}\n\n'
                    return
                if current.status != last_status:
                    data = ChatRequestSerializer(current).data
                    event = {
                        ChatRequest.Status.COMPLETED: "completed",
                        ChatRequest.Status.FAILED: "failed",
                        ChatRequest.Status.CANCELLED: "cancelled",
                    }.get(current.status, "status")
                    if event == "failed" and current.error_code == "AI_PROVIDER_NOT_CONFIGURED":
                        data["message"] = "JT-Code AI provider is not configured."
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
                    last_status = current.status
                if current.status in {
                    ChatRequest.Status.COMPLETED,
                    ChatRequest.Status.FAILED,
                    ChatRequest.Status.CANCELLED,
                }:
                    return
                yield "event: heartbeat\ndata: {}\n\n"
                time.sleep(1)
            yield (
                'event: timeout\ndata: {"code":"stream_timeout",'
                '"message":"Streaming window expired; poll the request endpoint."}\n\n'
            )

        response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["Connection"] = "keep-alive"
        response["X-Accel-Buffering"] = "no"
        return response
