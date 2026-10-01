from __future__ import annotations

import random
from datetime import timedelta
from uuid import uuid4

import sentry_sdk
from celery import shared_task
from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from apps.conversations.models import ChatRequest, Message
from apps.conversations.streaming import notify_chat_status
from apps.events.outbox import add_outbox_event

TERMINAL_STATUSES = {
    ChatRequest.Status.COMPLETED,
    ChatRequest.Status.FAILED,
    ChatRequest.Status.CANCELLED,
}
PERMANENT_GATEWAY_ERRORS = {
    "AI_PROVIDER_NOT_CONFIGURED",
    "AI_PROVIDER_UNSUPPORTED",
    "MODEL_NOT_FOUND",
    "MODEL_POLICY_NOT_FOUND",
    "MODEL_PROVIDER_INACTIVE",
    "INVALID_INPUT",
}


def retry_delay_seconds(retry_count: int) -> int:
    """Bound exponential retries so one unhealthy provider cannot flood a queue."""
    return min(300, 2 ** max(retry_count, 0)) + random.randint(0, 3)


def next_dispatch_time(delay_seconds: float = 0):
    """When the safety-net dispatcher may republish a request that has not started."""
    return timezone.now() + timedelta(seconds=delay_seconds + settings.CHAT_DISPATCH_GRACE_SECONDS)


def _emit_status_event(chat_request: ChatRequest, event_type: str) -> None:
    add_outbox_event(
        event_type,
        str(chat_request.id),
        {
            "requestId": str(chat_request.id),
            "conversationId": str(chat_request.conversation_id),
            "userId": str(chat_request.owner_id),
            "traceId": chat_request.trace_id,
            "status": chat_request.status,
            "errorCode": chat_request.error_code,
            "modelRunId": str(chat_request.model_run_id) if chat_request.model_run_id else None,
        },
        headers={"trace_id": chat_request.trace_id},
    )
    transaction.on_commit(lambda request_id=str(chat_request.id): notify_chat_status(request_id))


def _conversation_messages(chat_request: ChatRequest):
    """Load a bounded, chronological conversation context for the gateway."""
    from apps.ai_gateway.adapters import ChatMessage

    limit = max(1, settings.CHAT_MAX_CONTEXT_MESSAGES)
    messages = list(
        Message.objects.filter(
            conversation_id=chat_request.conversation_id,
            organization_id=chat_request.organization_id,
        ).order_by("-created_at", "-id")[:limit]
    )
    return [ChatMessage(role=message.role, content=message.content) for message in reversed(messages)]


def _latest_model_run_id(request_id: str):
    from apps.ai_gateway.models import ModelRun

    return (
        ModelRun.objects.filter(request_id=request_id)
        .order_by("-created_at")
        .values_list("id", flat=True)
        .first()
    )


def _fail_request(request_id: str, *, code: str, message: str) -> dict:
    with transaction.atomic():
        chat_request = ChatRequest.objects.select_for_update().get(id=request_id)
        if chat_request.status in TERMINAL_STATUSES:
            return {"status": chat_request.status, "idempotent": True}
        chat_request.status = ChatRequest.Status.FAILED
        chat_request.error_code = code
        chat_request.error_message = message[:2000]
        chat_request.model_run_id = _latest_model_run_id(request_id)
        chat_request.completed_at = timezone.now()
        chat_request.save(
            update_fields=(
                "status",
                "error_code",
                "error_message",
                "model_run",
                "completed_at",
                "updated_at",
            )
        )
        _emit_status_event(chat_request, "chat.request.failed")
    return {"status": "failed", "error_code": code}


def _retry_or_fail(task, request_id: str, exc: Exception, *, code: str) -> dict:
    with transaction.atomic():
        chat_request = ChatRequest.objects.select_for_update().get(id=request_id)
        if chat_request.status in TERMINAL_STATUSES or chat_request.cancel_requested_at is not None:
            return {"status": chat_request.status, "idempotent": True}
        chat_request.retry_count += 1
        chat_request.last_retry_at = timezone.now()
        chat_request.error_code = code
        chat_request.error_message = str(exc)[:2000]
        chat_request.model_run_id = _latest_model_run_id(request_id)
        if chat_request.retry_count > chat_request.max_retries:
            chat_request.status = ChatRequest.Status.FAILED
            chat_request.completed_at = timezone.now()
            chat_request.save(
                update_fields=(
                    "retry_count",
                    "last_retry_at",
                    "error_code",
                    "error_message",
                    "model_run",
                    "status",
                    "completed_at",
                    "updated_at",
                )
            )
            _emit_status_event(chat_request, "chat.request.failed")
            return {"status": "failed", "error_code": code}
        countdown = retry_delay_seconds(chat_request.retry_count)
        chat_request.status = ChatRequest.Status.QUEUED
        chat_request.dispatch_after = next_dispatch_time(countdown)
        chat_request.save(
            update_fields=(
                "retry_count",
                "last_retry_at",
                "error_code",
                "error_message",
                "model_run",
                "status",
                "dispatch_after",
                "updated_at",
            )
        )
        max_retries = chat_request.max_retries
        transaction.on_commit(lambda request_id=str(chat_request.id): notify_chat_status(request_id))
    raise task.retry(exc=exc, countdown=countdown, max_retries=max_retries) from exc


@shared_task(bind=True, acks_late=True, reject_on_worker_lost=True)
def process_chat_request(self, request_id: str) -> dict:
    """Generate a chat response through the governed AI gateway exactly once."""
    with transaction.atomic():
        request = (
            ChatRequest.objects.select_for_update().select_related("conversation", "owner").get(id=request_id)
        )
        if request.status in TERMINAL_STATUSES:
            return {"status": request.status, "idempotent": True}
        if request.cancel_requested_at is not None:
            request.status = ChatRequest.Status.CANCELLED
            request.completed_at = request.completed_at or timezone.now()
            request.save(update_fields=("status", "completed_at", "updated_at"))
            transaction.on_commit(lambda request_id=str(request.id): notify_chat_status(request_id))
            return {"status": "cancelled", "idempotent": True}
        if request.status == ChatRequest.Status.RUNNING:
            return {"status": "running", "idempotent": True}
        task_id = getattr(self.request, "id", "") or ""
        request.status = ChatRequest.Status.RUNNING
        request.started_at = timezone.now()
        if task_id:
            request.celery_task_id = task_id
        request.save(update_fields=("status", "started_at", "celery_task_id", "updated_at"))
        transaction.on_commit(lambda request_id=str(request.id): notify_chat_status(request_id))
    try:
        from apps.ai_gateway.service import generate_completion

        outcome = generate_completion(
            messages=_conversation_messages(request),
            task_type=request.task_type,
            request_id=str(request.id),
            trace_id=request.trace_id,
            organization_id=request.organization_id,
        )
        with transaction.atomic():
            request = (
                ChatRequest.objects.select_for_update().select_related("conversation").get(id=request_id)
            )
            if request.status in TERMINAL_STATUSES or request.cancel_requested_at is not None:
                return {"status": request.status, "idempotent": True}
            request.output_text = outcome.content
            request.status = ChatRequest.Status.COMPLETED
            request.error_code = ""
            request.error_message = ""
            request.provider_name = outcome.provider.name
            request.model_name = outcome.model.name
            request.model_run = outcome.run
            request.completed_at = timezone.now()
            request.save(
                update_fields=(
                    "output_text",
                    "status",
                    "error_code",
                    "error_message",
                    "provider_name",
                    "model_name",
                    "model_run",
                    "completed_at",
                    "updated_at",
                )
            )
            Message.objects.create(
                conversation=request.conversation,
                organization=request.organization,
                role=Message.Role.ASSISTANT,
                content=outcome.content,
                metadata={
                    "modelRunId": str(outcome.run.id),
                    "provider": outcome.provider.name,
                    "model": outcome.model.name,
                },
            )
            _emit_status_event(request, "chat.request.completed")
        return {"status": "completed", "model_run_id": str(outcome.run.id)}
    except Exception as exc:  # noqa: BLE001 - gateway failures must be durably surfaced
        from apps.ai_gateway.adapters import AIGatewayError

        sentry_sdk.capture_exception(exc)
        code = exc.code if isinstance(exc, AIGatewayError) else "CHAT_EXECUTION_FAILED"
        if code in PERMANENT_GATEWAY_ERRORS:
            return _fail_request(request_id, code=code, message=str(exc))
        return _retry_or_fail(self, request_id, exc, code=code)


@shared_task
def recover_stalled_chat_requests() -> int:
    """Requeue chat work stranded by a worker loss, failing requests that keep stalling.

    Each recovery consumes one retry, so a request that repeatedly kills its
    worker (for example by exhausting memory) cannot loop forever.
    """
    cutoff = timezone.now() - timedelta(seconds=settings.CHAT_REQUEST_STALLED_TIMEOUT_SECONDS)
    recovered = 0
    stalled_ids = list(
        ChatRequest.objects.filter(
            status=ChatRequest.Status.RUNNING,
            started_at__lt=cutoff,
            cancel_requested_at__isnull=True,
        ).values_list("id", flat=True)[:500]
    )
    for request_id in stalled_ids:
        task_id = str(uuid4())
        with transaction.atomic():
            request = (
                ChatRequest.objects.select_for_update(skip_locked=True)
                .filter(
                    id=request_id,
                    status=ChatRequest.Status.RUNNING,
                    started_at__lt=cutoff,
                    cancel_requested_at__isnull=True,
                )
                .first()
            )
            if request is None:
                continue
            request.retry_count += 1
            request.last_retry_at = timezone.now()
            if request.retry_count > request.max_retries:
                request.status = ChatRequest.Status.FAILED
                request.error_code = "WORKER_LOST"
                request.error_message = "The request repeatedly stalled and exhausted its retries."
                request.completed_at = timezone.now()
                request.save(
                    update_fields=(
                        "retry_count",
                        "last_retry_at",
                        "status",
                        "error_code",
                        "error_message",
                        "completed_at",
                        "updated_at",
                    )
                )
                _emit_status_event(request, "chat.request.failed")
                continue
            request.status = ChatRequest.Status.QUEUED
            request.error_code = "WORKER_RECOVERY"
            request.error_message = "Recovered after worker heartbeat timeout."
            request.celery_task_id = task_id
            request.dispatch_after = next_dispatch_time()
            request.save(
                update_fields=(
                    "retry_count",
                    "last_retry_at",
                    "status",
                    "error_code",
                    "error_message",
                    "celery_task_id",
                    "dispatch_after",
                    "updated_at",
                )
            )
            transaction.on_commit(lambda rid=str(request.id): notify_chat_status(rid))
        try:
            process_chat_request.apply_async(args=[str(request_id)], task_id=task_id)
        except Exception as exc:  # noqa: BLE001 - periodic dispatch retries durable requests
            sentry_sdk.capture_exception(exc)
        recovered += 1
    return recovered


@shared_task
def dispatch_queued_chat_requests() -> int:
    """Republish QUEUED requests whose publication was lost (e.g. broker outage).

    Only requests past ``dispatch_after`` are considered; the deadline is pushed
    forward before publishing so retries in backoff and recently published work
    are never duplicated.
    """
    now = timezone.now()
    dispatched = 0
    candidates = list(
        ChatRequest.objects.filter(
            status=ChatRequest.Status.QUEUED,
            cancel_requested_at__isnull=True,
        )
        .filter(models.Q(dispatch_after__isnull=True) | models.Q(dispatch_after__lte=now))
        .order_by("created_at")
        .values_list("id", "celery_task_id")[:100]
    )
    for request_id, existing_task_id in candidates:
        # Reuse the durable task id so one revoke() covers every copy of the message.
        task_id = existing_task_id or str(uuid4())
        claimed = (
            ChatRequest.objects.filter(id=request_id, status=ChatRequest.Status.QUEUED)
            .filter(models.Q(dispatch_after__isnull=True) | models.Q(dispatch_after__lte=now))
            .update(celery_task_id=task_id, dispatch_after=next_dispatch_time(), updated_at=now)
        )
        if not claimed:
            continue
        try:
            process_chat_request.apply_async(args=[str(request_id)], task_id=task_id)
        except Exception as exc:  # noqa: BLE001 - next beat tick retries publication
            sentry_sdk.capture_exception(exc)
            ChatRequest.objects.filter(id=request_id, celery_task_id=task_id).update(dispatch_after=now)
            continue
        dispatched += 1
    return dispatched
