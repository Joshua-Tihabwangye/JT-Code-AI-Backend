from config.settings.base import *  # noqa: F403,F405
from config.settings.base import ALLOWED_HOSTS as BASE_ALLOWED_HOSTS
from config.settings.secure import *  # noqa: F403
from config.settings.validation import validate_settings

DEBUG = False
DATABASES, CACHES, REDIS_URL, CELERY_BROKER_URL, CELERY_RESULT_BACKEND = runtime_connection_settings()  # noqa: F405
HEALTHCHECK_EXTERNAL_DEPENDENCIES = True
ALLOWED_HOSTS = list(dict.fromkeys([*BASE_ALLOWED_HOSTS, "localhost", "127.0.0.1"]))

validate_settings("production")
