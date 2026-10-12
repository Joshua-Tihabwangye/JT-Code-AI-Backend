from config.settings.base import *  # noqa: F403,F405
from config.settings.base import ALLOWED_HOSTS as BASE_ALLOWED_HOSTS
from config.settings.secure import *  # noqa: F403
from config.settings.validation import validate_settings

DEBUG = False
DATABASES, CACHES, REDIS_URL, CELERY_BROKER_URL, CELERY_RESULT_BACKEND = runtime_connection_settings()  # noqa: F405
HEALTHCHECK_EXTERNAL_DEPENDENCIES = True
ALLOWED_HOSTS = BASE_ALLOWED_HOSTS
# In Kubernetes, probes and Prometheus address the pod by IP (downward API POD_IP).
if POD_IP := env("POD_IP"):  # noqa: F405
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, POD_IP]

# Docker Compose: Prometheus, health checks and sibling containers call the API by its service
# name or loopback (DJANGO_INTERNAL_HOSTS, set by the compose files), not the public host.
ALLOWED_HOSTS = [*ALLOWED_HOSTS, *env_list("DJANGO_INTERNAL_HOSTS")]  # noqa: F405

validate_settings("production")
