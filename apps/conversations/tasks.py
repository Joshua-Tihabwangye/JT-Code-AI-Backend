from celery import shared_task
from django.db import transaction
import sentry_sdk
from apps.conversations.models import ChatRequest, Message
from apps.conversations.providers import AIProviderNotConfigured, get_text_provider
from apps.events.outbox import add_outbox_event

@shared_task(bind=True, autoretry_for=(TimeoutError,), retry_backoff=True, retry_jitter=True, max_retries=3)
def process_chat_request(self, request_id: str) -> None:
    request = ChatRequest.objects.select_related('conversation', 'owner').get(id=request_id)
    if request.status not in {ChatRequest.Status.QUEUED, ChatRequest.Status.RUNNING}:
        return
    request.status = ChatRequest.Status.RUNNING
    request.save(update_fields=('status', 'updated_at'))
    try:
        output = get_text_provider().generate(request.input_text)
        with transaction.atomic():
            request.output_text = output
            request.status = ChatRequest.Status.COMPLETED
            request.error_code = ''
            request.save(update_fields=('output_text', 'status', 'error_code', 'updated_at'))
            Message.objects.create(conversation=request.conversation, role=Message.Role.ASSISTANT, content=output)
            add_outbox_event('chat.request.completed', str(request.id), {
                'requestId': str(request.id), 'conversationId': str(request.conversation_id),
                'userId': str(request.owner_id), 'traceId': request.trace_id,
            })
    except AIProviderNotConfigured as exc:
        request.status = ChatRequest.Status.FAILED
        request.error_code = 'AI_PROVIDER_NOT_CONFIGURED'
        request.save(update_fields=('status', 'error_code', 'updated_at'))
        sentry_sdk.capture_exception(exc)
    except Exception as exc:
        request.status = ChatRequest.Status.FAILED
        request.error_code = 'AI_PROVIDER_FAILURE'
        request.save(update_fields=('status', 'error_code', 'updated_at'))
        sentry_sdk.capture_exception(exc)
        raise
