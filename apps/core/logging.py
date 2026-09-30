from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from apps.core.context import request_id_var, trace_id_var


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.trace_id = trace_id_var.get()
        return True


class JSONFormatter(logging.Formatter):
    """Render logs as one JSON object per line without request-body data."""

    _SAFE_EXTRA_FIELDS = frozenset(
        {"method", "path", "status_code", "duration_ms", "dependency", "ready", "checks", "workflow_id"}
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_var.get()),
            "trace_id": getattr(record, "trace_id", trace_id_var.get()),
        }
        for name in self._SAFE_EXTRA_FIELDS:
            if hasattr(record, name):
                payload[name] = getattr(record, name)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))
