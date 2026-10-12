"""CI profile: real PostgreSQL and Redis, offline task execution."""

from django.core.exceptions import ImproperlyConfigured

from config.settings.test import *  # noqa: F403,F405

DATABASES, CACHES, REDIS_URL, CELERY_BROKER_URL, CELERY_RESULT_BACKEND = runtime_connection_settings()  # noqa: F405
if not (REDIS_URL and CELERY_BROKER_URL and CELERY_RESULT_BACKEND):
    # Fail at startup, not at the first cache call: this profile exists to exercise real Redis.
    raise ImproperlyConfigured(
        "The ci profile requires REDIS_URL, CELERY_BROKER_URL and CELERY_RESULT_BACKEND."
    )
