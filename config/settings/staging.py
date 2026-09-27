from config.settings.base import *  # noqa: F403,F405
from config.settings.secure import *  # noqa: F403
from config.settings.validation import validate_settings

# Staging exercises production behavior; debug pages must never be internet-facing.
DEBUG = False
DATABASES, CACHES, REDIS_URL, CELERY_BROKER_URL, CELERY_RESULT_BACKEND = runtime_connection_settings()  # noqa: F405
HEALTHCHECK_EXTERNAL_DEPENDENCIES = True

validate_settings("staging")
