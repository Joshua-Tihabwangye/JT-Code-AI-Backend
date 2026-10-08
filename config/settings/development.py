from config.settings.base import *  # noqa: F403,F405
from config.settings.base import runtime_connection_settings
from config.settings.validation import validate_settings

SECRET_KEY = env("DJANGO_SECRET_KEY", "unsafe-local-development-key-change-me")  # noqa: F405
DEBUG = env_bool("DJANGO_DEBUG", True)  # noqa: F405
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")  # noqa: F405
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "http://localhost:5173")  # noqa: F405
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", "http://localhost:5173")  # noqa: F405
DATABASES, CACHES, REDIS_URL, CELERY_BROKER_URL, CELERY_RESULT_BACKEND = runtime_connection_settings(
    development=True
)  # noqa: F405
SUPABASE_STORAGE_PREFIX = env("SUPABASE_STORAGE_PREFIX", "jt-code/development")  # noqa: F405
KAFKA_BOOTSTRAP_SERVERS = env("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")  # noqa: F405
KAFKA_SECURITY_PROTOCOL = env("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT")  # noqa: F405
KAFKA_TOPIC_PREFIX = env("KAFKA_TOPIC_PREFIX", "jt-code.dev")  # noqa: F405
N8N_BASE_URL = env("N8N_BASE_URL", "http://localhost:5678")  # noqa: F405
N8N_CALLBACK_BASE_URL = env("N8N_CALLBACK_BASE_URL", "http://localhost:8000/api/v1")  # noqa: F405
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

validate_settings("development")
