"""JSON logging configuration with request and trace correlation."""

from __future__ import annotations

from typing import Any

LOGGING: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "apps.core.logging.JSONFormatter"}},
    "filters": {"request_context": {"()": "apps.core.logging.RequestContextFilter"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "json", "filters": ["request_context"]}
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
