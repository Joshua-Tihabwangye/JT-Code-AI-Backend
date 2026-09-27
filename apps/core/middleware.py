from __future__ import annotations

import re
import uuid
from collections.abc import Callable

import sentry_sdk
from django.http import HttpRequest, HttpResponse

from apps.core.context import request_id_var, trace_id_var

_SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RequestContextMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    @staticmethod
    def _identifier(value: str | None) -> str:
        return value if value and _SAFE_CORRELATION_ID.fullmatch(value) else str(uuid.uuid4())

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = self._identifier(request.headers.get("X-Request-ID"))
        trace_id = self._identifier(request.headers.get("X-Trace-ID"))
        request.request_id = request_id  # type: ignore[attr-defined]
        request.trace_id = trace_id  # type: ignore[attr-defined]
        request_token = request_id_var.set(request_id)
        trace_token = trace_id_var.set(trace_id)
        try:
            sentry_sdk.set_tag("request_id", request_id)
            sentry_sdk.set_tag("trace_id", trace_id)
            response = self.get_response(request)
            response["X-Request-ID"] = request_id
            response["X-Trace-ID"] = trace_id
            return response
        finally:
            request_id_var.reset(request_token)
            trace_id_var.reset(trace_token)
