from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import dj_database_url
import sentry_sdk
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.redis import RedisIntegration

from config.logging import LOGGING  # noqa: F401

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise ImproperlyConfigured(f"Missing required environment variable: {name}")
    return value or ""


def env_bool(name: str, default: bool = False) -> bool:
    return env(name, str(default)).lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


def env_float(name: str, default: float) -> float:
    return float(env(name, str(default)))


SECRET_KEY = env("DJANGO_SECRET_KEY", "")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "apps.core",
    "apps.identity",
    "apps.conversations",
    "apps.assets",
    "apps.events",
    "apps.jobs",
    "apps.knowledge",
    "apps.billing",
    "apps.governance",
    "apps.integrations",
    "apps.ai_gateway",
    "apps.agents",
    "apps.documents",
    "apps.conversions",
]

MIDDLEWARE = [
    "apps.core.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": dj_database_url.config(
        conn_max_age=int(env("DATABASE_CONN_MAX_AGE", "60")),
        conn_health_checks=True,
    )
}

AUTH_USER_MODEL = "identity.User"
AUTH_PASSWORD_VALIDATORS: list[dict[str, Any]] = []
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "http://localhost:5173")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", "http://localhost:5173")
CORS_ALLOW_CREDENTIALS = False

REDIS_URL = env("REDIS_URL", "redis://localhost:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "TIMEOUT": 300,
        "OPTIONS": {"socket_connect_timeout": 3, "socket_timeout": 3},
        "KEY_PREFIX": "jt-code",
    }
}

CELERY_BROKER_URL = env("CELERY_BROKER_URL", "redis://localhost:6379/1")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 600
CELERY_TASK_SOFT_TIME_LIMIT = 540
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BEAT_SCHEDULE = {
    "publish-kafka-outbox": {
        "task": "apps.events.tasks.publish_outbox_batch",
        "schedule": 2.0,
    },
    "process-job-callbacks": {
        "task": "apps.jobs.tasks.process_callbacks",
        "schedule": 30.0,
    },
    "check-job-deadlines": {
        "task": "apps.jobs.tasks.check_job_deadlines",
        "schedule": 60.0,
    },
    "sync-knowledge-sources": {
        "task": "apps.knowledge.tasks.sync_sources",
        "schedule": 300.0,
    },
    "process-billing-webhooks": {
        "task": "apps.billing.tasks.process_webhooks",
        "schedule": 60.0,
    },
    "expire-old-jobs": {
        "task": "apps.jobs.tasks.expire_old_jobs",
        "schedule": 3600.0,
    },
    "cleanup-old-audit-events": {
        "task": "apps.governance.tasks.cleanup_old_audit_events",
        "schedule": 86400.0,
    },
}

SUPABASE_URL = env("SUPABASE_URL")
SUPABASE_JWT_SECRET = env("SUPABASE_JWT_SECRET")
SUPABASE_JWT_AUDIENCE = env("SUPABASE_JWT_AUDIENCE")
SUPABASE_JWT_ISSUER = env("SUPABASE_JWT_ISSUER")
SUPABASE_WEBHOOK_SIGNING_SECRET = env("SUPABASE_WEBHOOK_SIGNING_SECRET")

IMAGEKIT_PUBLIC_KEY = env("IMAGEKIT_PUBLIC_KEY")
IMAGEKIT_PRIVATE_KEY = env("IMAGEKIT_PRIVATE_KEY")
IMAGEKIT_ENDPOINT_URL = env("IMAGEKIT_ENDPOINT_URL")
IMAGEKIT_UPLOAD_FOLDER = env("IMAGEKIT_UPLOAD_FOLDER", "jt-code/development")
IMAGEKIT_MAX_UPLOAD_BYTES = int(env("IMAGEKIT_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
IMAGEKIT_UPLOAD_AUTH_TTL_SECONDS = int(env("IMAGEKIT_UPLOAD_AUTH_TTL_SECONDS", "300"))

KAFKA_BOOTSTRAP_SERVERS = env("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_CLIENT_ID = env("KAFKA_CLIENT_ID", "jt-code-api")
KAFKA_SECURITY_PROTOCOL = env("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT")
KAFKA_SASL_MECHANISM = env("KAFKA_SASL_MECHANISM")
KAFKA_SASL_USERNAME = env("KAFKA_SASL_USERNAME")
KAFKA_SASL_PASSWORD = env("KAFKA_SASL_PASSWORD")
KAFKA_TOPIC_PREFIX = env("KAFKA_TOPIC_PREFIX", "jt-code.dev")

AI_PROVIDER = env("AI_PROVIDER", "disabled")
N8N_SENTRY_RELAY_SECRET = env("N8N_SENTRY_RELAY_SECRET")

# n8n Integration
N8N_BASE_URL = env("N8N_BASE_URL", "http://localhost:5678")
N8N_API_KEY = env("N8N_API_KEY")
N8N_WEBHOOK_SECRET = env("N8N_WEBHOOK_SECRET")
N8N_CALLBACK_BASE_URL = env("N8N_CALLBACK_BASE_URL", "http://localhost:8000/api/v1")
N8N_WORKFLOW_PREFIX = env("N8N_WORKFLOW_PREFIX", "jt-code")

# AI Gateway
AI_GATEWAY_DEFAULT_POLICY = env("AI_GATEWAY_DEFAULT_POLICY", "balanced")
AI_GATEWAY_MAX_COST_USD = env_float("AI_GATEWAY_MAX_COST_USD", 10.0)
AI_GATEWAY_MAX_LATENCY_MS = int(env("AI_GATEWAY_MAX_LATENCY_MS", "30000"))
AI_GATEWAY_FALLBACK_ENABLED = env_bool("AI_GATEWAY_FALLBACK_ENABLED", True)

# Agent Runtime
AGENT_MAX_ITERATIONS = int(env("AGENT_MAX_ITERATIONS", "6"))

# Billing
BILLING_CREDIT_VALUE_USD = env_float("BILLING_CREDIT_VALUE_USD", 0.01)
BILLING_FX_BUFFER = env_float("BILLING_FX_BUFFER", 1.05)
BILLING_MARGIN_MULTIPLIER = env_float("BILLING_MARGIN_MULTIPLIER", 1.25)
BILLING_DEFAULT_PLAN = env("BILLING_DEFAULT_PLAN", "free")
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET")
STRIPE_PUBLISHABLE_KEY = env("STRIPE_PUBLISHABLE_KEY")

# Knowledge/RAG
# Supabase PostgreSQL (pgvector) is the vector store. The `vector` extension is
# enabled in a data migration that is a no-op on non-PostgreSQL test databases.
PGVECTOR_ENABLED = env_bool("PGVECTOR_ENABLED", True)
VECTOR_EMBEDDING_DIMENSIONS = int(env("VECTOR_EMBEDDING_DIMENSIONS", "1536"))
VECTOR_MIN_SIMILARITY = env_float("VECTOR_MIN_SIMILARITY", 0.0)
RAG_EMBEDDING_PROVIDER = env("RAG_EMBEDDING_PROVIDER", "openai")
RAG_EMBEDDING_MODEL = env("RAG_EMBEDDING_MODEL", "text-embedding-3-small")
RAG_CHUNK_SIZE = int(env("RAG_CHUNK_SIZE", "1000"))
RAG_CHUNK_OVERLAP = int(env("RAG_CHUNK_OVERLAP", "200"))
RAG_TOP_K = int(env("RAG_TOP_K", "10"))
RAG_RERANK_TOP_K = int(env("RAG_RERANK_TOP_K", "5"))
RAG_SIMILARITY_THRESHOLD = env_float("RAG_SIMILARITY_THRESHOLD", 0.7)
RAG_MAX_EXTRACTED_BYTES = int(env("RAG_MAX_EXTRACTED_BYTES", str(5 * 1024 * 1024)))
RAG_URL_FETCH_TIMEOUT_SECONDS = float(env("RAG_URL_FETCH_TIMEOUT_SECONDS", "30"))

# Embedding provider credentials (server-side only). Only the configured
# provider is loaded at runtime; the extras are dependency-free placeholders.
OPENAI_API_KEY = env("OPENAI_API_KEY")
GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_EMBEDDING_MODEL = env("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")

# Governance
AUDIT_EVENT_RETENTION_DAYS = int(env("AUDIT_EVENT_RETENTION_DAYS", "2555"))
SAFETY_EVENT_RETENTION_DAYS = int(env("SAFETY_EVENT_RETENTION_DAYS", "2555"))
CONSENT_VERSION = env("CONSENT_VERSION", "1.0")

# Integrations
WEBHOOK_MAX_RETRIES = int(env("WEBHOOK_MAX_RETRIES", "5"))
WEBHOOK_RETRY_BASE_DELAY = int(env("WEBHOOK_RETRY_BASE_DELAY", "60"))
KAFKA_CONSUMER_GROUP_PREFIX = env("KAFKA_CONSUMER_GROUP_PREFIX", "jt-code")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.identity.authentication.SupabaseJWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_THROTTLE_RATES": {
        "chat": env("THROTTLE_CHAT", "60/hour"),
        "images": env("THROTTLE_IMAGES", "30/hour"),
        "embeddings": env("THROTTLE_EMBEDDINGS", "120/hour"),
        "conversions": env("THROTTLE_CONVERSIONS", "20/hour"),
        "research": env("THROTTLE_RESEARCH", "10/hour"),
        "burst": env("THROTTLE_BURST", "30/minute"),
    },
}
SPECTACULAR_SETTINGS = {
    "TITLE": "JT-Code API",
    "DESCRIPTION": "Django API for JT-Code web and React Native clients.",
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SECURITY": [{"SupabaseBearer": []}],
    "COMPONENT_SPLIT_REQUEST": True,
}

HEALTHCHECK_EXTERNAL_DEPENDENCIES = env_bool("HEALTHCHECK_EXTERNAL_DEPENDENCIES", False)

SENTRY_DSN = env("SENTRY_DSN")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=env("SENTRY_ENVIRONMENT"),
        release=env("SENTRY_RELEASE", "jt-code-api@0.1.0"),
        integrations=[DjangoIntegration(), CeleryIntegration(), RedisIntegration()],
        traces_sample_rate=env_float("SENTRY_TRACES_SAMPLE_RATE", 0.1),
        profiles_sample_rate=env_float("SENTRY_PROFILES_SAMPLE_RATE", 0.0),
        send_default_pii=False,
        max_request_body_size="never",
    )
