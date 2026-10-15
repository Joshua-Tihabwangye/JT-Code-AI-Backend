"""Content negotiation for Server-Sent Events endpoints."""

from __future__ import annotations

import json
from typing import Any

from rest_framework.renderers import BaseRenderer


class EventStreamRenderer(BaseRenderer):
    """Lets SSE endpoints accept ``Accept: text/event-stream``, the header SSE clients send.

    The stream itself is a ``StreamingHttpResponse``; this renderer only formats
    responses produced before streaming starts (401, 404, throttling) as an SSE
    ``error`` event, so the client can parse them like any other event.
    """

    media_type = "text/event-stream"
    format = "event-stream"
    charset = "utf-8"

    def render(
        self, data: Any, accepted_media_type: str | None = None, renderer_context: Any = None
    ) -> bytes:
        if data is None:
            return b""
        if isinstance(data, bytes):
            return data
        payload = data if isinstance(data, str) else json.dumps(data, default=str)
        return f"event: error\ndata: {payload}\n\n".encode()
