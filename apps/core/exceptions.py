from rest_framework.response import Response
from rest_framework.views import exception_handler

def api_exception_handler(exc: Exception, context: dict) -> Response | None:
    response = exception_handler(exc, context)
    if response is None:
        return None
    request = context.get('request')
    trace_id = getattr(request, 'trace_id', None)
    if isinstance(response.data, dict):
        response.data.setdefault('traceId', trace_id)
    else:
        response.data = {'detail': response.data, 'traceId': trace_id}
    return response
