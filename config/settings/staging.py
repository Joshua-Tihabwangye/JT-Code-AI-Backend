from config.settings.base import *  # noqa: F403,F405
from config.settings.secure import *  # noqa: F403
from config.settings.validation import validate_settings

# Staging exercises production behavior; debug pages must never be internet-facing.
DEBUG = False
DATABASES, CACHES, REDIS_URL, CELERY_BROKER_URL, CELERY_RESULT_BACKEND = runtime_connection_settings()  # noqa: F405
HEALTHCHECK_EXTERNAL_DEPENDENCIES = True
# In Kubernetes, probes and Prometheus address the pod by IP (downward API POD_IP).
if POD_IP := env("POD_IP"):  # noqa: F405
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, POD_IP]  # noqa: F405

# Docker Compose: Prometheus, health checks and sibling containers call the API by its service
# name or loopback (DJANGO_INTERNAL_HOSTS, set by the compose files), not the public host.
ALLOWED_HOSTS = [*ALLOWED_HOSTS, *env_list("DJANGO_INTERNAL_HOSTS")]  # noqa: F405

# The OpenAPI schema and Swagger UI describe every endpoint; outside development only
# staff may read them.
SPECTACULAR_SETTINGS = {  # noqa: F405
    **SPECTACULAR_SETTINGS,  # noqa: F405
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAdminUser"],
}

validate_settings("staging")
