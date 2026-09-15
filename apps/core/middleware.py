import uuid
from collections.abc import Callable
from django.http import HttpRequest, HttpResponse
from sentry_sdk import configure_scope
from apps.core.context import request_id_var, trace_id_var

class RequestContextMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = request.headers.get('X-Request-ID') or str(uuid.uuid4())
        trace_id = request.headers.get('X-Trace-ID') or str(uuid.uuid4())
        request.request_id = request_id  # type: ignore[attr-defined]
        request.trace_id = trace_id  # type: ignore[attr-defined]
        request_id_token = request_id_var.set(request_id)
        trace_id_token = trace_id_var.set(trace_id)
        try:
            with configure_scope() as scope:
                scope.set_tag('request_id', request_id)
                scope.set_tag('trace_id', trace_id)
            response = self.get_response(request)
            response['X-Request-ID'] = request_id
            response['X-Trace-ID'] = trace_id
            return response
        finally:
            request_id_var.reset(request_id_token)
            trace_id_var.reset(trace_id_token)
