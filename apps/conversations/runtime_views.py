from __future__ import annotations

import asyncio
import hashlib
import json

import sentry_sdk
from asgiref.sync import sync_to_async
from celery import current_app
from django.conf import settings
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



def _dispatch_chat_request(chat_request_id: str) -> None:
    """Enqueue after commit and retain the broker task id for cancellation/audit."""
    try:
        result = process_chat_request.delay(chat_request_id)
    except Exception as exc:  # noqa: BLE001 - beat will retry a safely persisted request
        sentry_sdk.capture_exception(exc)
        return
    task_id = getattr(result, "id", "")
    if task_id:
        ChatRequest.objects.filter(
            id=chat_request_id, status=ChatRequest.Status.QUEUED
        ).update(celery_task_id=task_id)


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
            transaction.on_commit(lambda request_id=str(chat_request.id): _dispatch_chat_request(request_id))
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

    @action(detail=True, methods=["post"])
    def cancel(self, request: Request, pk=None) -> Response:
        """Cancel queued/running chat work without exposing broker ids to clients."""
        item = self.get_object()
        with transaction.atomic():
            item = ChatRequest.objects.select_for_update().get(id=item.id)
            if item.status not in {
                ChatRequest.Status.COMPLETED,
                ChatRequest.Status.FAILED,
                ChatRequest.Status.CANCELLED,
            }:
                item.cancel_requested_at = timezone.now()
                item.status = ChatRequest.Status.CANCELLED
                item.error_code = ""
                item.error_message = ""
                item.completed_at = item.completed_at or timezone.now()
                item.save(
                    update_fields=(
                        "cancel_requested_at",
                        "status",
                        "error_code",
                        "error_message",
                        "completed_at",
                        "updated_at",
                    )
                )
                add_outbox_event(
                    "chat.request.cancelled",
                    str(item.id),
                    {
                        "requestId": str(item.id),
                        "conversationId": str(item.conversation_id),
                        "userId": str(item.owner_id),
                        "traceId": item.trace_id,
                        "status": item.status,
                    },
                )
                if item.celery_task_id:
                    transaction.on_commit(
                        lambda task_id=item.celery_task_id: current_app.control.revoke(task_id)
                    )
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=["get"])
    def stream(self, request: Request, pk=None) -> StreamingHttpResponse:
        item = self.get_object()
        request_id = str(item.id)
        last_event_id = request.headers.get("Last-Event-ID", "")

        def load_state():
            """Run ORM work in Django's sync DB lane; close connections per SSE poll."""
            try:
                close_old_connections()
                current = tenant_scoped_queryset(
                    ChatRequest.objects.filter(id=request_id), request.user
                ).first()
                if current is None:
                    return None
                event_id = f"{current.updated_at.timestamp():.6f}:{current.status}"
                return current, event_id
            finally:
                close_old_connections()

        async def event_stream():
            """ASGI-safe SSE: no request thread sleeps while a response is pending."""
            loop = asyncio.get_running_loop()
            deadline = loop.time() + max(1, settings.CHAT_SSE_MAX_SECONDS)
            poll_interval = max(0.1, settings.CHAT_SSE_POLL_SECONDS)
            heartbeat_interval = max(poll_interval, settings.CHAT_SSE_HEARTBEAT_SECONDS)
            emitted_event_id = last_event_id
            last_heartbeat = loop.time()
            while loop.time() < deadline:
                state = await sync_to_async(load_state, thread_sensitive=True)()
                if state is None:
                    yield 'event: failed\ndata: {"code":"not_found","message":"Request not found."}\n\n'
                    return
                current, event_id = state
                terminal = current.status in {
                    ChatRequest.Status.COMPLETED,
                    ChatRequest.Status.FAILED,
                    ChatRequest.Status.CANCELLED,
                }
                if event_id != emitted_event_id:
                    data = ChatRequestSerializer(current).data
                    event = {
                        ChatRequest.Status.COMPLETED: "completed",
                        ChatRequest.Status.FAILED: "failed",
                        ChatRequest.Status.CANCELLED: "cancelled",
                    }.get(current.status, "status")
                    yield f"id: {event_id}\nevent: {event}\ndata: {json.dumps(data)}\n\n"
                    emitted_event_id = event_id
                if terminal:
                    return
                now = loop.time()
                if now - last_heartbeat >= heartbeat_interval:
                    yield "event: heartbeat\ndata: {}\n\n"
                    last_heartbeat = now
                await asyncio.sleep(min(poll_interval, max(0, deadline - loop.time())))
            yield (
                'event: timeout\ndata: {"code":"stream_timeout",'
                '"message":"Streaming window expired; poll the request endpoint."}\n\n'
            )

        response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache, no-store"
        response["Connection"] = "keep-alive"
        response["X-Accel-Buffering"] = "no"
        response["X-Content-Type-Options"] = "nosniff"
        return response
