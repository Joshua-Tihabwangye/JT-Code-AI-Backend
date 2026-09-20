"""Central logging configuration.

Target repository structure places logging configuration in
``config/logging.py``. The dictionary is imported by the base settings module
so only one definition exists. Structured records carry ``request_id`` and
``trace_id`` populated by ``apps.core.logging.RequestContextFilter`` from
per-request contextvars.
"""

from __future__ import annotations

from typing import Any

LOGGING: dict[str, Any] = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'jsonish': {
            'format': (
                '%(asctime)s %(levelname)s %(name)s '
                'request_id=%(request_id)s trace_id=%(trace_id)s %(message)s'
            )
        },
    },
    'filters': {'request_context': {'()': 'apps.core.logging.RequestContextFilter'}},
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'jsonish',
            'filters': ['request_context'],
        }
    },
    'root': {'handlers': ['console'], 'level': 'INFO'},
}