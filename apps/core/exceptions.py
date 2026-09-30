from typing import Any

from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import exception_handler


class IdempotencyConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This idempotency key was already used for a different request."
    default_code = "idempotency_conflict"


class ArchivedConversation(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Archived conversations do not accept new messages; unarchive it first."
    default_code = "conversation_archived"


def _message(data: object) -> str:
    if isinstance(data, dict) and "detail" in data:
        return str(data["detail"])
    return "Request validation failed." if isinstance(data, dict) else str(data)


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """Return the versioned public error envelope for DRF-raised API errors."""
    response = exception_handler(exc, context)
    if response is None:
        return None
    request = context.get("request")
    code = getattr(exc, "default_code", "request_error")
    response.data = {
        "code": code,
        "message": _message(response.data),
        "details": response.data,
        "requestId": getattr(request, "request_id", None),
    }
    return response
