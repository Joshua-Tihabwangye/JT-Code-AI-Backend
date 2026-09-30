"""CI profile: real PostgreSQL and Redis, offline task execution."""

from config.settings.test import *  # noqa: F403,F405

DATABASES, CACHES, REDIS_URL, CELERY_BROKER_URL, CELERY_RESULT_BACKEND = runtime_connection_settings()  # noqa: F405
