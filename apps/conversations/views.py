from __future__ import annotations

import json
import time

from django.db import IntegrityError, close_old_connections, transaction
from django.http import StreamingHttpResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from apps.conversations.models import ChatRequest, Conversation, Message
from apps.conversations.serializers import (
    ChatRequestCreateSerializer,
    ChatRequestSerializer,
    ConversationSerializer,
)
from apps.conversations.tasks import process_chat_request
from apps.core.throttling import BurstThrottle, ChatThrottle
from apps.events.outbox import add_outbox_event


class ConversationViewSet(viewsets.ModelViewSet):
    serializer_class = ConversationSerializer
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']
    def get_queryset(self):
        return Conversation.objects.filter(owner=self.request.user).order_by('-updated_at')
    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

class ChatRequestViewSet(viewsets.GenericViewSet):
    serializer_class = ChatRequestSerializer
    throttle_classes = [ChatThrottle, BurstThrottle]
    def get_queryset(self):
        return ChatRequest.objects.filter(owner=self.request.user).select_related('conversation')

    def create(self, request: Request) -> Response:
        serializer = ChatRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conversation = Conversation.objects.filter(
            id=serializer.validated_data['conversationId'], owner=request.user
        ).first()
        if not conversation:
            return Response({'detail': 'Conversation not found.'}, status=status.HTTP_404_NOT_FOUND)
        idempotency_key = request.headers.get('Idempotency-Key')
        if not idempotency_key:
            return Response(
            {'detail': 'Idempotency-Key header is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
        try:
            with transaction.atomic():
                chat_request = ChatRequest.objects.create(
                    owner=request.user,
                    conversation=conversation,
                    idempotency_key=idempotency_key,
                    input_text=serializer.validated_data['chatInput'],
                    timezone=serializer.validated_data.get('timezone', ''),
                    locale=serializer.validated_data.get('locale', ''),
                    trace_id=getattr(request, 'trace_id', ''),
                )
                Message.objects.create(
                    conversation=conversation, role=Message.Role.USER,
                    content=chat_request.input_text,
                )
                add_outbox_event('chat.request.accepted', str(chat_request.id), {
                    'requestId': str(chat_request.id), 'conversationId': str(conversation.id),
                    'userId': str(request.user.id), 'traceId': chat_request.trace_id,
                })
                transaction.on_commit(lambda: process_chat_request.delay(str(chat_request.id)))
        except IntegrityError:
            chat_request = ChatRequest.objects.get(owner=request.user, idempotency_key=idempotency_key)
        return Response(ChatRequestSerializer(chat_request).data, status=status.HTTP_202_ACCEPTED)

    def retrieve(self, request: Request, pk=None) -> Response:
        item = self.get_object()
        return Response(ChatRequestSerializer(item).data)

    @action(detail=True, methods=['get'], url_path='stream')
    def stream(self, request: Request, pk=None):
        item = self.get_object()
        request_id = str(item.id)
        owner_id = request.user.id

        def event_stream():
            last_status = None
            started = time.monotonic()
            while time.monotonic() - started < 90:
                close_old_connections()
                current = ChatRequest.objects.filter(id=request_id, owner_id=owner_id).first()
                if not current:
                    yield 'event: failed\ndata: {"message":"Request not found."}\n\n'
                    return
                if current.status != last_status or current.status in {
                    ChatRequest.Status.COMPLETED, ChatRequest.Status.FAILED
                }:
                    data = ChatRequestSerializer(current).data
                    event = (
                        'completed' if current.status == ChatRequest.Status.COMPLETED
                        else 'failed' if current.status == ChatRequest.Status.FAILED
                        else 'status'
                    )
                    if event == 'failed' and current.error_code == 'AI_PROVIDER_NOT_CONFIGURED':
                        data['message'] = 'JT-Code AI provider is not configured.'
                    yield f'event: {event}\ndata: {json.dumps(data)}\n\n'
                    last_status = current.status
                if current.status in {
                        ChatRequest.Status.COMPLETED, ChatRequest.Status.FAILED,
                        ChatRequest.Status.CANCELLED
                    }:
                    return
                yield 'event: heartbeat\ndata: {}\n\n'
                time.sleep(1)
            payload = '{"message":"Streaming window expired; poll the request endpoint."}'
            yield f'event: failed\ndata: {payload}\n\n'

        response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
        response['Cache-Control'] = 'no-cache'
        response['X-Accel-Buffering'] = 'no'
        return response
