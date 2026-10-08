from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv
from kombu import Queue

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
    "apps.identity.apps.IdentityConfig",
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
    "apps.tools",
    "apps.documents",
    "apps.conversions",
    "apps.analytics",
    "apps.usage",
    "apps.orchestration",
    "apps.operations",
]

MIDDLEWARE = [
    "apps.core.middleware.RequestContextMiddleware",
    "apps.core.metrics.MetricsMiddleware",
    "apps.core.edge.EdgeProtectionMiddleware",
    "apps.core.edge.ReadOnlyModeMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "apps.core.edge.SecurityHeadersMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.governance.audit.AuditTrailMiddleware",
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


def runtime_connection_settings(
    *, development: bool = False, database_url: str | None = None
) -> tuple[dict[str, Any], dict[str, Any], str, str, str]:
    """Build stateful connection settings for a named environment profile.

    Supabase PostgreSQL is required for every deployable profile. Development
    may opt into local Redis only; it never falls back to SQLite.
    """
    resolved_database_url = database_url if database_url is not None else env("DATABASE_URL")
    if not resolved_database_url:
        raise ImproperlyConfigured("DATABASE_URL is required; configure Supabase PostgreSQL explicitly.")
    redis_default = "redis://localhost:6379/0" if development else ""
    broker_default = "redis://localhost:6379/1" if development else ""
    result_default = "redis://localhost:6379/2" if development else ""
    redis_url = env("REDIS_URL", redis_default)
    broker_url = env("CELERY_BROKER_URL", broker_default)
    result_url = env("CELERY_RESULT_BACKEND", result_default)
    pooler_mode = env("DATABASE_POOLER_MODE", "direct").lower()
    conn_max_age = int(env("DATABASE_CONN_MAX_AGE", "60"))
    if pooler_mode == "transaction":
        conn_max_age = 0
    # Parse the explicitly selected profile URL. ``dj_database_url.config``
    # would silently prefer DATABASE_URL from the process environment, which
    # can make the test profile connect to the development database even when
    # TEST_DATABASE_URL was supplied.
    database_config = dj_database_url.parse(
        resolved_database_url,
        conn_max_age=conn_max_age,
        conn_health_checks=True,
    )
    if database_config.get("ENGINE", "").endswith("postgresql"):
        options = database_config.setdefault("OPTIONS", {})
        options.setdefault("connect_timeout", int(env("DATABASE_CONNECT_TIMEOUT_SECONDS", "10")))
        if sslrootcert := env("DATABASE_SSLROOTCERT"):
            options.setdefault("sslrootcert", sslrootcert)
        options.setdefault("application_name", env("DATABASE_APPLICATION_NAME", "jt-code-api"))
        if pooler_mode == "transaction":
            database_config.setdefault("DISABLE_SERVER_SIDE_CURSORS", True)
    database = {"default": database_config}

    def redis_cache(key_prefix: str, timeout: int) -> dict[str, Any]:
        return {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": redis_url,
            "TIMEOUT": timeout,
            "OPTIONS": {"socket_connect_timeout": 3, "socket_timeout": 3},
            "KEY_PREFIX": key_prefix,
        }

    caches = {
        "default": redis_cache("jt-code:cache", 300),
        "rate_limits": redis_cache("jt-code:rate-limit", 3600),
        "job_locks": redis_cache("jt-code:job-lock", 900),
    }
    return database, caches, redis_url, broker_url, result_url


# A profile must replace these through ``runtime_connection_settings``.
DATABASES: dict[str, Any] = {}

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

CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")
CORS_ALLOW_CREDENTIALS = False

CACHES: dict[str, Any] = {}
REDIS_URL = ""
CELERY_BROKER_URL = ""
CELERY_RESULT_BACKEND = ""
JOB_STALLED_TIMEOUT_SECONDS = int(env("JOB_STALLED_TIMEOUT_SECONDS", "660"))
CHAT_REQUEST_STALLED_TIMEOUT_SECONDS = int(env("CHAT_REQUEST_STALLED_TIMEOUT_SECONDS", "660"))
CHAT_MAX_CONTEXT_MESSAGES = int(env("CHAT_MAX_CONTEXT_MESSAGES", "40"))
CHAT_SSE_HEARTBEAT_SECONDS = float(env("CHAT_SSE_HEARTBEAT_SECONDS", "15"))
CHAT_SSE_MAX_SECONDS = int(env("CHAT_SSE_MAX_SECONDS", "300"))
CHAT_SSE_RECONCILIATION_SECONDS = float(env("CHAT_SSE_RECONCILIATION_SECONDS", "30"))
# How long a QUEUED chat request may wait (after publish/backoff) before the
# safety-net dispatcher republishes it.
CHAT_DISPATCH_GRACE_SECONDS = int(env("CHAT_DISPATCH_GRACE_SECONDS", "60"))
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 600
CELERY_TASK_SOFT_TIME_LIMIT = 540
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BEAT_SCHEDULE = {
    "detect-cost-anomalies": {
        "task": "apps.usage.tasks.detect_cost_anomalies",
        "schedule": 3600.0,
    },
    "sweep-n8n-workflows": {
        "task": "apps.orchestration.tasks.sweep_workflows",
        "schedule": 60.0,
    },
    "run-due-automations": {
        "task": "apps.orchestration.tasks.run_due_automations",
        "schedule": 60.0,
    },
    "reconcile-n8n-executions": {
        "task": "apps.orchestration.tasks.reconcile_n8n_executions",
        "schedule": 600.0,
    },
    "prune-n8n-callbacks": {
        "task": "apps.orchestration.tasks.prune_workflow_callbacks",
        "schedule": 86400.0,
    },
    "publish-kafka-outbox": {
        "task": "apps.events.tasks.publish_outbox_batch",
        "schedule": 2.0,
        "options": {"expires": 10},
    },
    "prune-published-outbox-events": {
        "task": "apps.events.tasks.prune_published_outbox_events",
        "schedule": 3600.0,
        "options": {"expires": 600},
    },
    "process-job-callbacks": {
        "task": "apps.jobs.tasks.process_callbacks",
        "schedule": 30.0,
        "options": {"expires": 30},
    },
    "check-job-deadlines": {
        "task": "apps.jobs.tasks.check_job_deadlines",
        "schedule": 60.0,
        "options": {"expires": 60},
    },
    "sync-knowledge-sources": {
        "task": "apps.knowledge.tasks.sync_sources",
        "schedule": 300.0,
    },
    "settle-finished-usage-reservations": {
        "task": "apps.usage.tasks.settle_finished_reservations",
        "schedule": 60.0,
        "options": {"expires": 60},
    },
    "reconcile-stripe-billing": {
        "task": "apps.billing.tasks.reconcile_stripe_billing",
        "schedule": 3600.0,
        "options": {"expires": 3600},
    },
    "retry-failed-stripe-events": {
        "task": "apps.billing.tasks.retry_failed_stripe_events",
        "schedule": 300.0,
        "options": {"expires": 300},
    },
    "run-auto-topups": {
        "task": "apps.billing.tasks.run_auto_topups",
        "schedule": 600.0,
        "options": {"expires": 600},
    },
    "reconcile-provider-usage": {
        "task": "apps.usage.tasks.reconcile_provider_usage",
        "schedule": 86400.0,
        "options": {"expires": 21600},
    },
    "recover-stalled-knowledge-ingestion": {
        "task": "apps.knowledge.tasks.recover_stalled_ingestion",
        "schedule": 600.0,
        "options": {"expires": 600},
    },
    "purge-deleted-assets": {
        "task": "apps.assets.tasks.purge_deleted_assets",
        "schedule": 3600.0,
        "options": {"expires": 3600},
    },
    "reconcile-supabase-storage-assets": {
        "task": "apps.assets.tasks.reconcile_assets",
        "schedule": 3600.0,
        "options": {"expires": 3600},
    },
    "sweep-supabase-storage-orphans": {
        "task": "apps.assets.tasks.sweep_orphans",
        "schedule": 86400.0,
        "options": {"expires": 21600},
    },
    "expire-asset-upload-intents": {
        "task": "apps.assets.tasks.expire_upload_intents",
        "schedule": 300.0,
        "options": {"expires": 300},
    },
    "grant-free-plan-credits": {
        "task": "apps.billing.tasks.grant_free_plan_credits",
        "schedule": 86400.0,
        "options": {"expires": 21600},
    },
    "billing-renewal-notices": {
        "task": "apps.billing.tasks.check_subscription_renewals",
        "schedule": 86400.0,
        "options": {"expires": 21600},
    },
    "recover-stalled-jobs": {
        "task": "apps.jobs.tasks.recover_stalled_jobs",
        "schedule": 60.0,
        "options": {"expires": 60},
    },
    "dispatch-queued-jobs": {
        "task": "apps.jobs.tasks.dispatch_queued_jobs",
        "schedule": 30.0,
        "options": {"expires": 30},
    },
    "expire-tool-approvals": {
        "task": "apps.tools.tasks.expire_tool_approvals",
        "schedule": 300.0,
        "options": {"expires": 300},
    },
    "recover-stalled-agent-runs": {
        "task": "apps.agents.tasks.recover_stalled_agent_runs",
        "schedule": 60.0,
        "options": {"expires": 60},
    },
    "recover-stalled-analytics": {
        "task": "apps.analytics.tasks.recover_stalled_analytics",
        "schedule": 300.0,
        "options": {"expires": 300},
    },
    "recover-stalled-chat-requests": {
        "task": "apps.conversations.tasks.recover_stalled_chat_requests",
        "schedule": 60.0,
        "options": {"expires": 60},
    },
    "dispatch-queued-chat-requests": {
        "task": "apps.conversations.tasks.dispatch_queued_chat_requests",
        "schedule": 30.0,
        "options": {"expires": 30},
    },
    "expire-old-jobs": {
        "task": "apps.jobs.tasks.expire_old_jobs",
        "schedule": 3600.0,
    },
    "cleanup-old-audit-events": {
        "task": "apps.governance.tasks.cleanup_old_audit_events",
        "schedule": 86400.0,
        "options": {"expires": 600},
    },
}


CELERY_TASK_DEFAULT_QUEUE = "jobs.default"
CELERY_TASK_QUEUES = (
    Queue("jobs.default"),
    Queue("jobs.analysis"),
    Queue("jobs.ingestion"),
    Queue("jobs.visualization"),
    Queue("analytics.analysis"),
    Queue("analytics.visualization"),
    Queue("orchestration"),
)
CELERY_TASK_ROUTES = {
    "apps.analytics.tasks.execute_analysis_run": {"queue": "analytics.analysis"},
    "apps.analytics.tasks.execute_visualization": {"queue": "analytics.visualization"},
    "apps.analytics.tasks.recover_stalled_analytics": {"queue": "jobs.default"},
    "apps.assets.tasks.*": {"queue": "jobs.default"},
    "apps.events.tasks.*": {"queue": "jobs.default"},
    "apps.jobs.tasks.execute_job_task": {"queue": "jobs.analysis"},
    "apps.knowledge.tasks.*": {"queue": "jobs.ingestion"},
    "apps.documents.*": {"queue": "jobs.visualization"},
    "apps.conversions.*": {"queue": "jobs.visualization"},
    "apps.conversations.tasks.*": {"queue": "jobs.analysis"},
    "apps.agents.tasks.*": {"queue": "jobs.analysis"},
    "apps.tools.tasks.*": {"queue": "jobs.default"},
    "apps.usage.tasks.*": {"queue": "jobs.default"},
    "apps.orchestration.tasks.*": {"queue": "orchestration"},
}
CELERY_TASK_DEFAULT_DELIVERY_MODE = "persistent"
CELERY_TASK_RESULT_EXPIRES = 3600
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600}
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
SUPABASE_URL = env("SUPABASE_URL")
SUPABASE_INTERNAL_URL = env("SUPABASE_INTERNAL_URL", SUPABASE_URL)
SUPABASE_JWKS_URL = env("SUPABASE_JWKS_URL")
SUPABASE_JWT_SECRET = env("SUPABASE_JWT_SECRET")
SUPABASE_JWT_VERIFICATION = env("SUPABASE_JWT_VERIFICATION", "jwks").lower()
SUPABASE_JWT_AUDIENCE = env("SUPABASE_JWT_AUDIENCE")
SUPABASE_JWT_ISSUER = env("SUPABASE_JWT_ISSUER")
SUPABASE_WEBHOOK_SIGNING_SECRET = env("SUPABASE_WEBHOOK_SIGNING_SECRET")
# Server-only key for the Supabase Auth Admin API (never expose to clients).
SUPABASE_SECRET_KEY = env("SUPABASE_SECRET_KEY")
SUPABASE_ALLOW_ANONYMOUS_USERS = env_bool("SUPABASE_ALLOW_ANONYMOUS_USERS", False)

# Asset bytes live in a private Supabase Storage bucket.  The service-role key
# is already represented by SUPABASE_SECRET_KEY and must remain server-only.
SUPABASE_STORAGE_BUCKET = env("SUPABASE_STORAGE_BUCKET", "jt-code-assets")
SUPABASE_STORAGE_PREFIX = env("SUPABASE_STORAGE_PREFIX", "jt-code")
# Empty means ``${SUPABASE_URL}/storage/v1``; override only for a trusted proxy.
SUPABASE_STORAGE_API_URL = env("SUPABASE_STORAGE_API_URL")
SUPABASE_STORAGE_PUBLIC_API_URL = env("SUPABASE_STORAGE_PUBLIC_API_URL")
ASSET_MAX_UPLOAD_BYTES = int(env("ASSET_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
ASSET_UPLOAD_AUTH_TTL_SECONDS = int(env("ASSET_UPLOAD_AUTH_TTL_SECONDS", "300"))
ASSET_SIGNED_URL_TTL_SECONDS = int(env("ASSET_SIGNED_URL_TTL_SECONDS", "900"))
SUPABASE_STORAGE_TIMEOUT_SECONDS = float(env("SUPABASE_STORAGE_TIMEOUT_SECONDS", "30"))
ASSET_DELETE_GRACE_DAYS = int(env("ASSET_DELETE_GRACE_DAYS", "7"))
ASSET_ORPHAN_GRACE_HOURS = int(env("ASSET_ORPHAN_GRACE_HOURS", "24"))
ASSET_RECONCILE_PAGE_SIZE = int(env("ASSET_RECONCILE_PAGE_SIZE", "100"))
ASSET_RECONCILE_MAX_PAGES = int(env("ASSET_RECONCILE_MAX_PAGES", "100"))
ASSET_LOCAL_FALLBACK_ENABLED = env_bool("ASSET_LOCAL_FALLBACK_ENABLED", False)
# Upload content types are verified against the file's magic bytes. SVG is
# deliberately absent: it can carry script.
_DEFAULT_ASSET_TYPES = (
    "image/png,image/jpeg,image/gif,image/webp,application/pdf,application/json,application/zip,"
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
    "text/csv,text/markdown,text/plain"
)
ASSET_ALLOWED_CONTENT_TYPES = tuple(env_list("ASSET_ALLOWED_CONTENT_TYPES", _DEFAULT_ASSET_TYPES))
# Reconcile verifies READY assets in batches, re-checking each at most this often.
ASSET_RECONCILE_BATCH_SIZE = int(env("ASSET_RECONCILE_BATCH_SIZE", "200"))
ASSET_RECONCILE_INTERVAL_HOURS = int(env("ASSET_RECONCILE_INTERVAL_HOURS", "24"))
ASSET_RECONCILE_MAX_DEPTH = int(env("ASSET_RECONCILE_MAX_DEPTH", "6"))
ASSET_DELETE_MAX_ATTEMPTS = int(env("ASSET_DELETE_MAX_ATTEMPTS", "10"))

# Phase 12 analytics workers run only a bounded declarative transform language.
# Deploy analysis and visualization queues in separate containers with matching
# CPU/memory limits; Celery also enforces the wall-clock limits below.
ANALYTICS_ALLOWED_MIME_TYPES = tuple(env_list("ANALYTICS_ALLOWED_MIME_TYPES", "text/csv,application/csv"))
ANALYTICS_MAX_INLINE_BYTES = int(env("ANALYTICS_MAX_INLINE_BYTES", str(5 * 1024 * 1024)))
ANALYTICS_MAX_DATASET_BYTES = int(env("ANALYTICS_MAX_DATASET_BYTES", str(25 * 1024 * 1024)))
ANALYTICS_MAX_RESULT_BYTES = int(env("ANALYTICS_MAX_RESULT_BYTES", str(25 * 1024 * 1024)))
ANALYTICS_MAX_DATASET_ROWS = int(env("ANALYTICS_MAX_DATASET_ROWS", "100000"))
ANALYTICS_MAX_DATASET_COLUMNS = int(env("ANALYTICS_MAX_DATASET_COLUMNS", "200"))
ANALYTICS_MAX_DATASET_CELLS = int(env("ANALYTICS_MAX_DATASET_CELLS", "2000000"))
ANALYTICS_RESULT_PREVIEW_ROWS = int(env("ANALYTICS_RESULT_PREVIEW_ROWS", "100"))
ANALYTICS_MAX_CHART_POINTS = int(env("ANALYTICS_MAX_CHART_POINTS", "10000"))
ANALYTICS_MAX_PLOTLY_SPEC_BYTES = int(env("ANALYTICS_MAX_PLOTLY_SPEC_BYTES", str(5 * 1024 * 1024)))
ANALYTICS_DOWNLOAD_TIMEOUT_SECONDS = int(env("ANALYTICS_DOWNLOAD_TIMEOUT_SECONDS", "30"))
ANALYTICS_TASK_SOFT_TIME_LIMIT_SECONDS = int(env("ANALYTICS_TASK_SOFT_TIME_LIMIT_SECONDS", "270"))
ANALYTICS_TASK_TIME_LIMIT_SECONDS = int(env("ANALYTICS_TASK_TIME_LIMIT_SECONDS", "300"))
ANALYTICS_STALLED_AFTER_MINUTES = int(env("ANALYTICS_STALLED_AFTER_MINUTES", "15"))
# Isolated engine process limits (address space, CPU seconds, wall clock).
ANALYTICS_SANDBOX_MEMORY_MB = int(env("ANALYTICS_SANDBOX_MEMORY_MB", "1536"))
ANALYTICS_SANDBOX_CPU_SECONDS = int(env("ANALYTICS_SANDBOX_CPU_SECONDS", "240"))
ANALYTICS_SANDBOX_TIMEOUT_SECONDS = int(env("ANALYTICS_SANDBOX_TIMEOUT_SECONDS", "250"))
# Plotly specs up to this size are also kept inline on the visualization row;
# the full spec is always stored as a private Supabase Storage asset.
ANALYTICS_INLINE_SPEC_BYTES = int(env("ANALYTICS_INLINE_SPEC_BYTES", "262144"))

KAFKA_BOOTSTRAP_SERVERS = env("KAFKA_BOOTSTRAP_SERVERS")
KAFKA_CLIENT_ID = env("KAFKA_CLIENT_ID", "jt-code-api")
KAFKA_SECURITY_PROTOCOL = env("KAFKA_SECURITY_PROTOCOL")
KAFKA_SASL_MECHANISM = env("KAFKA_SASL_MECHANISM")
KAFKA_SASL_USERNAME = env("KAFKA_SASL_USERNAME")
KAFKA_SASL_PASSWORD = env("KAFKA_SASL_PASSWORD")
KAFKA_TOPIC_PREFIX = env("KAFKA_TOPIC_PREFIX", "jt-code")
EVENT_OUTBOX_MAX_ATTEMPTS = int(env("EVENT_OUTBOX_MAX_ATTEMPTS", "10"))
EVENT_OUTBOX_MAX_BACKOFF_SECONDS = int(env("EVENT_OUTBOX_MAX_BACKOFF_SECONDS", "300"))
EVENT_OUTBOX_LEASE_SECONDS = int(env("EVENT_OUTBOX_LEASE_SECONDS", "60"))
EVENT_OUTBOX_RETENTION_DAYS = int(env("EVENT_OUTBOX_RETENTION_DAYS", "7"))
KAFKA_CONSUMER_MAX_ATTEMPTS = int(env("KAFKA_CONSUMER_MAX_ATTEMPTS", "5"))
KAFKA_CONSUMER_RETRY_MAX_SECONDS = int(env("KAFKA_CONSUMER_RETRY_MAX_SECONDS", "30"))
KAFKA_TOPIC_PARTITIONS = int(env("KAFKA_TOPIC_PARTITIONS", "3"))
KAFKA_TOPIC_REPLICATION_FACTOR = int(env("KAFKA_TOPIC_REPLICATION_FACTOR", "3"))
KAFKA_TOPIC_RETENTION_MS = int(env("KAFKA_TOPIC_RETENTION_MS", str(7 * 24 * 3600 * 1000)))

AI_PROVIDER = env("AI_PROVIDER", "disabled")
N8N_SENTRY_RELAY_SECRET = env("N8N_SENTRY_RELAY_SECRET")

# n8n orchestration (Phase 16; docs/N8N_ORCHESTRATION.md, infra/n8n).
# N8N_BASE_URL serves the editor and public API (workflow sync); webhooks are
# dispatched to N8N_WEBHOOK_BASE_URL (the queue-mode webhook processors).
N8N_BASE_URL = env("N8N_BASE_URL")
N8N_WEBHOOK_BASE_URL = env("N8N_WEBHOOK_BASE_URL")
N8N_API_KEY = env("N8N_API_KEY")
# Django -> n8n requests are signed with N8N_DISPATCH_SECRET; n8n -> Django
# callbacks with N8N_WEBHOOK_SECRET (previous value accepted during rotation).
N8N_DISPATCH_SECRET = env("N8N_DISPATCH_SECRET")
N8N_WEBHOOK_SECRET = env("N8N_WEBHOOK_SECRET")
N8N_WEBHOOK_SECRET_PREVIOUS = env("N8N_WEBHOOK_SECRET_PREVIOUS")
# Public base URL of this API as n8n reaches it, e.g. https://api.example.com/api/v1.
N8N_CALLBACK_BASE_URL = env("N8N_CALLBACK_BASE_URL")
N8N_WORKFLOW_PREFIX = env("N8N_WORKFLOW_PREFIX", "jt-code")
N8N_WORKFLOWS_DIR = env("N8N_WORKFLOWS_DIR", str(BASE_DIR / "n8n" / "workflows"))
N8N_REQUEST_TIMEOUT_SECONDS = int(env("N8N_REQUEST_TIMEOUT_SECONDS", "15"))
N8N_RETRY_BASE_SECONDS = int(env("N8N_RETRY_BASE_SECONDS", "30"))
N8N_RETRY_MAX_SECONDS = int(env("N8N_RETRY_MAX_SECONDS", "900"))
# Ids of the n8n credentials the workflows use (substituted on `n8n_workflows push`).
_N8N_CREDENTIALS = {
    "N8N_CREDENTIAL_SLACK": env("N8N_CREDENTIAL_SLACK"),
    "N8N_CREDENTIAL_SMTP": env("N8N_CREDENTIAL_SMTP"),
    "N8N_CREDENTIAL_GOOGLE_DRIVE": env("N8N_CREDENTIAL_GOOGLE_DRIVE"),
    "N8N_CREDENTIAL_NOTION": env("N8N_CREDENTIAL_NOTION"),
    "N8N_CREDENTIAL_GITHUB": env("N8N_CREDENTIAL_GITHUB"),
}
N8N_CREDENTIAL_IDS = {name: value for name, value in _N8N_CREDENTIALS.items() if value}

# AI Gateway
AI_GATEWAY_DEFAULT_POLICY = env("AI_GATEWAY_DEFAULT_POLICY", "balanced")
AI_GATEWAY_MAX_COST_USD = env_float("AI_GATEWAY_MAX_COST_USD", 10.0)
AI_GATEWAY_MAX_LATENCY_MS = int(env("AI_GATEWAY_MAX_LATENCY_MS", "30000"))
AI_GATEWAY_FALLBACK_ENABLED = env_bool("AI_GATEWAY_FALLBACK_ENABLED", True)
# Clients address aliases, never provider model names (Phase 7).
AI_DEFAULT_MODEL_ALIAS = env("AI_DEFAULT_MODEL_ALIAS", "default-chat")
AI_REQUEST_TIMEOUT_SECONDS = int(env("AI_REQUEST_TIMEOUT_SECONDS", "60"))
AI_MAX_RETRIES = int(env("AI_MAX_RETRIES", "2"))
AI_RETRY_BASE_SECONDS = env_float("AI_RETRY_BASE_SECONDS", 0.5)
AI_RETRY_MAX_BACKOFF_SECONDS = env_float("AI_RETRY_MAX_BACKOFF_SECONDS", 8.0)
AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS = int(env("AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "30"))
# Gemini (Generative Language REST API).
GEMINI_API_BASE = env("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta")
GEMINI_DEFAULT_MODEL = env("GEMINI_DEFAULT_MODEL", "gemini-2.5-flash")
# Images: Imagen generates, a Gemini image model edits, the chat model describes.
IMAGE_PROVIDER = env("IMAGE_PROVIDER")  # "gemini" (default) or "echo" (development/tests only)
GEMINI_IMAGE_MODEL = env("GEMINI_IMAGE_MODEL", "imagen-3.0-generate-002")
GEMINI_IMAGE_EDIT_MODEL = env("GEMINI_IMAGE_EDIT_MODEL", "gemini-2.5-flash-image")
GEMINI_SAFETY_THRESHOLD = env("GEMINI_SAFETY_THRESHOLD", "BLOCK_MEDIUM_AND_ABOVE")
# Llama via any hosted or self-hosted OpenAI-compatible endpoint.
LLAMA_API_BASE = env("LLAMA_API_BASE")
LLAMA_API_KEY = env("LLAMA_API_KEY")
LLAMA_DEFAULT_MODEL = env("LLAMA_DEFAULT_MODEL")

# Agent Runtime (LangGraph). Platform ceilings: agent definitions can only lower them.
AGENT_MAX_ITERATIONS = int(env("AGENT_MAX_ITERATIONS", "6"))
LANGGRAPH_MAX_STEPS = int(env("LANGGRAPH_MAX_STEPS", "20"))
AGENT_MAX_TOOL_CALLS = int(env("AGENT_MAX_TOOL_CALLS", "10"))
AGENT_MAX_COST_USD = env_float("AGENT_MAX_COST_USD", 0.50)
AGENT_MAX_DURATION_SECONDS = int(env("AGENT_MAX_DURATION_SECONDS", "300"))
AGENT_ROUTER_MODE = env("AGENT_ROUTER_MODE", "rules")
AGENT_RUN_STALLED_TIMEOUT_SECONDS = int(env("AGENT_RUN_STALLED_TIMEOUT_SECONDS", "660"))
AGENT_RUN_MAX_ATTEMPTS = int(env("AGENT_RUN_MAX_ATTEMPTS", "3"))
MAX_CONCURRENT_AGENT_RUNS_PER_TENANT = int(env("MAX_CONCURRENT_AGENT_RUNS_PER_TENANT", "10"))

# Tools, MCP and integrations (Phase 9)
# Fernet keys (comma-separated; first encrypts, all decrypt) for tool credentials at rest.
TOOL_CREDENTIALS_ENCRYPTION_KEYS = env_list("TOOL_CREDENTIALS_ENCRYPTION_KEYS")
EXTERNAL_API_TIMEOUT_SECONDS = int(env("EXTERNAL_API_TIMEOUT_SECONDS", "20"))
TOOL_MAX_RESPONSE_BYTES = int(env("TOOL_MAX_RESPONSE_BYTES", str(1024 * 1024)))
TOOL_MAX_OUTPUT_CHARS = int(env("TOOL_MAX_OUTPUT_CHARS", "20000"))
TOOL_MAX_ARGUMENT_BYTES = int(env("TOOL_MAX_ARGUMENT_BYTES", str(256 * 1024)))
TOOL_EGRESS_DENYLIST = env_list("TOOL_EGRESS_DENYLIST", "metadata.google.internal,*.internal,*.local")
TOOL_APPROVAL_TTL_SECONDS = int(env("TOOL_APPROVAL_TTL_SECONDS", "86400"))
BROWSER_TOOL_ENABLED = env_bool("BROWSER_TOOL_ENABLED", False)
SEARCH_API_BASE = env("SEARCH_API_BASE", "https://api.search.brave.com/res/v1/web/search")
SEARCH_API_KEY = env("SEARCH_API_KEY")
GITHUB_API_BASE = env("GITHUB_API_BASE", "https://api.github.com")
GITHUB_APP_ID = env("GITHUB_APP_ID")
GITHUB_APP_PRIVATE_KEY = env("GITHUB_APP_PRIVATE_KEY")
TOOL_GITHUB_BRANCH_PREFIX = env("TOOL_GITHUB_BRANCH_PREFIX", "jt-code/")
SLACK_API_BASE = env("SLACK_API_BASE", "https://slack.com/api")
ENABLE_MCP = env_bool("ENABLE_MCP", True)
MCP_TOOL_TIMEOUT_SECONDS = int(env("MCP_TOOL_TIMEOUT_SECONDS", "30"))

# Billing
BILLING_CREDIT_VALUE_USD = env_float("BILLING_CREDIT_VALUE_USD", 0.01)
BILLING_FX_BUFFER = env_float("BILLING_FX_BUFFER", 1.05)
BILLING_MARGIN_MULTIPLIER = env_float("BILLING_MARGIN_MULTIPLIER", 1.25)
BILLING_DEFAULT_PLAN = env("BILLING_DEFAULT_PLAN", "free")

# Usage metering (Phase 13). Credits are reserved before work starts and settled
# afterwards from recorded provider cost (cost_usd x FX buffer x margin / credit
# value) or the feature's flat price; a settlement never exceeds its reservation.
_DEFAULT_USAGE_RESERVATIONS = (
    "chat_messages=10,rag_queries=25,search_queries=40,knowledge_documents=100,image_generations=100,"
    "document_renders=20,file_conversions=15,workflow_executions=10,agent_runs=50,analysis_runs=10,api_calls=1"
)
USAGE_RESERVATION_CREDITS = env("USAGE_RESERVATION_CREDITS", _DEFAULT_USAGE_RESERVATIONS)
# Flat prices for features without metered provider cost (and the minimum for AI features).
_DEFAULT_USAGE_FLAT = (
    "chat_messages=1,rag_queries=2,search_queries=5,knowledge_documents=10,image_generations=100,"
    "document_renders=20,file_conversions=15,workflow_executions=10,agent_runs=2,analysis_runs=10,api_calls=1"
)
USAGE_FLAT_CREDITS = env("USAGE_FLAT_CREDITS", _DEFAULT_USAGE_FLAT)
USAGE_RESERVATION_TTL_MINUTES = int(env("USAGE_RESERVATION_TTL_MINUTES", "120"))
USAGE_RECONCILIATION_DRIFT_RATIO = env_float("USAGE_RECONCILIATION_DRIFT_RATIO", 0.01)
# Per-tenant concurrency ceilings (a plan's ``limits`` may override each key).
MAX_CONCURRENT_JOBS_PER_TENANT = int(env("MAX_CONCURRENT_JOBS_PER_TENANT", "20"))
MAX_CONCURRENT_CHAT_REQUESTS_PER_TENANT = int(env("MAX_CONCURRENT_CHAT_REQUESTS_PER_TENANT", "20"))
MAX_CONCURRENT_ANALYSIS_RUNS_PER_TENANT = int(env("MAX_CONCURRENT_ANALYSIS_RUNS_PER_TENANT", "5"))
# Tenant-wide request ceiling = per-user scope rate x this multiplier (plan-overridable).
THROTTLE_TENANT_MULTIPLIER = int(env("THROTTLE_TENANT_MULTIPLIER", "10"))
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET")
STRIPE_PUBLISHABLE_KEY = env("STRIPE_PUBLISHABLE_KEY")
# Pinned so a change to the Stripe account's default API version cannot change
# the payload shapes this code parses.
STRIPE_API_VERSION = env("STRIPE_API_VERSION", "2023-10-16")
STRIPE_WEBHOOK_TOLERANCE_SECONDS = int(env("STRIPE_WEBHOOK_TOLERANCE_SECONDS", "300"))
STRIPE_EVENT_MAX_ATTEMPTS = int(env("STRIPE_EVENT_MAX_ATTEMPTS", "8"))
# Frontend origin for Checkout/Portal return URLs; client-supplied return URLs
# must share an allowed origin (this plus CORS_ALLOWED_ORIGINS).
FRONTEND_URL = env("FRONTEND_URL", "http://localhost:5173").rstrip("/")
BILLING_TOPUP_MIN_CENTS = int(env("BILLING_TOPUP_MIN_CENTS", "500"))
BILLING_TOPUP_MAX_CENTS = int(env("BILLING_TOPUP_MAX_CENTS", "100000"))
# Auto top-up charges the saved default payment method at most once per window.
BILLING_AUTO_TOPUP_COOLDOWN_MINUTES = int(env("BILLING_AUTO_TOPUP_COOLDOWN_MINUTES", "60"))

# Knowledge/RAG
# Supabase PostgreSQL (pgvector) is the only vector store. The `vector` extension
# is enabled by the knowledge.0003 migration; there is no disabled mode.
VECTOR_EMBEDDING_DIMENSIONS = int(env("VECTOR_EMBEDDING_DIMENSIONS", "1536"))
# Minimum cosine similarity for a semantic candidate (0 keeps every candidate and
# lets hybrid fusion and reranking decide).
VECTOR_MIN_SIMILARITY = env_float("VECTOR_MIN_SIMILARITY", 0.0)
RAG_EMBEDDING_PROVIDER = env("RAG_EMBEDDING_PROVIDER", "gemini")
RAG_EMBEDDING_MODEL = env("RAG_EMBEDDING_MODEL", "text-embedding-3-small")
RAG_EMBEDDING_BATCH_SIZE = int(env("RAG_EMBEDDING_BATCH_SIZE", "64"))
RAG_CHUNK_SIZE = int(env("RAG_CHUNK_SIZE", "1000"))
RAG_CHUNK_OVERLAP = int(env("RAG_CHUNK_OVERLAP", "200"))
# Default result count for search; final evidence count for grounded answers.
RAG_TOP_K = int(env("RAG_TOP_K", "10"))
RAG_RERANK_TOP_K = int(env("RAG_RERANK_TOP_K", "5"))
RAG_HYBRID_CANDIDATES = int(env("RAG_HYBRID_CANDIDATES", "30"))
# "model" reranks/judges through the AI gateway alias below; "deterministic"
# uses the offline scorer only. Model failures fall back and are reported.
RAG_RERANKER = env("RAG_RERANKER", "model")
RAG_RERANK_MODEL_ALIAS = env("RAG_RERANK_MODEL_ALIAS", "classification")
RAG_JUDGE = env("RAG_JUDGE", "model")
RAG_JUDGE_MODEL_ALIAS = env("RAG_JUDGE_MODEL_ALIAS", "classification")
RAG_MAX_CONTEXT_TOKENS = int(env("RAG_MAX_CONTEXT_TOKENS", "6000"))
RAG_EVAL_MIN_RECALL = env_float("RAG_EVAL_MIN_RECALL", 0.8)
RAG_EVAL_MIN_MRR = env_float("RAG_EVAL_MIN_MRR", 0.5)
RAG_MAX_EXTRACTED_BYTES = int(env("RAG_MAX_EXTRACTED_BYTES", str(5 * 1024 * 1024)))
RAG_URL_FETCH_TIMEOUT_SECONDS = float(env("RAG_URL_FETCH_TIMEOUT_SECONDS", "30"))
RAG_EMBEDDING_TIMEOUT_SECONDS = float(env("RAG_EMBEDDING_TIMEOUT_SECONDS", "30"))
RAG_EMBEDDING_MAX_RETRIES = int(env("RAG_EMBEDDING_MAX_RETRIES", "2"))
# Sources/documents stuck mid-ingestion longer than this are failed and requeued.
RAG_INGESTION_STALLED_MINUTES = int(env("RAG_INGESTION_STALLED_MINUTES", "30"))
RAG_INGESTION_MAX_RETRIES = int(env("RAG_INGESTION_MAX_RETRIES", "3"))

# Embedding provider credentials (server-side only).
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
WEBHOOK_RETRY_MAX_SECONDS = int(env("WEBHOOK_RETRY_MAX_SECONDS", "3600"))
WEBHOOK_DELIVERY_TIMEOUT_SECONDS = float(env("WEBHOOK_DELIVERY_TIMEOUT_SECONDS", "10"))
WEBHOOK_ALLOWED_HOSTS = env_list("WEBHOOK_ALLOWED_HOSTS")
WEBHOOK_SIGNING_SECRET = env("WEBHOOK_SIGNING_SECRET")
KAFKA_CONSUMER_GROUP_PREFIX = env("KAFKA_CONSUMER_GROUP_PREFIX", "jt-code")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.identity.authentication.SupabaseJWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "apps.core.schema.JTCodeAutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_THROTTLE_CLASSES": ["apps.core.throttling.IPRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        "ip": env("THROTTLE_IP", "300/minute"),
        "chat": env("THROTTLE_CHAT", "60/hour"),
        "images": env("THROTTLE_IMAGES", "30/hour"),
        "embeddings": env("THROTTLE_EMBEDDINGS", "120/hour"),
        "conversions": env("THROTTLE_CONVERSIONS", "20/hour"),
        "research": env("THROTTLE_RESEARCH", "10/hour"),
        "burst": env("THROTTLE_BURST", "30/minute"),
        "agent_runs": env("THROTTLE_AGENT_RUNS", "20/hour"),
        "analytics": env("THROTTLE_ANALYTICS", "30/hour"),
    },
}
SPECTACULAR_SETTINGS = {
    "TITLE": "JT-Code API",
    "DESCRIPTION": "Django API for JT-Code web and React Native clients.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SECURITY": [{"SupabaseBearer": []}],
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        "AssetVisibility": "apps.assets.models.Asset.Visibility",
        "UsageFeature": "apps.usage.models.Feature",
        "EntitlementFeature": "apps.billing.models.Entitlement.FeatureType",
        "VisualizationKind": "apps.analytics.models.Visualization.Kind",
        "KnowledgeDocumentVisibility": "apps.knowledge.models.Document.Visibility",
        "ModelStatus": [
            ("active", "Active"),
            ("deprecated", "Deprecated"),
            ("disabled", "Disabled"),
            ("beta", "Beta"),
        ],
        "EvaluationType": [
            ("accuracy", "Accuracy"),
            ("faithfulness", "Faithfulness"),
            ("hallucination", "Hallucination"),
            ("toxicity", "Toxicity"),
            ("bias", "Bias"),
            ("latency", "Latency"),
            ("cost", "Cost"),
            ("custom", "Custom"),
        ],
        "PromptCategory": [
            ("system", "System Prompt"),
            ("task", "Task Prompt"),
            ("template", "Template"),
            ("chain_of_thought", "Chain of Thought"),
            ("few_shot", "Few-shot Examples"),
            ("guardrail", "Guardrail"),
        ],
        "PlanStatus": [("active", "Active"), ("archived", "Archived")],
        "QueuedRunStatus": [
            ("queued", "Queued"),
            ("running", "Running"),
            ("completed", "Completed"),
            ("failed", "Failed"),
        ],
        "ConsentStatus": [
            ("granted", "Granted"),
            ("denied", "Denied"),
            ("withdrawn", "Withdrawn"),
            ("expired", "Expired"),
        ],
        "SupportCaseStatus": [
            ("open", "Open"),
            ("in_progress", "In Progress"),
            ("waiting_user", "Waiting for User"),
            ("waiting_third_party", "Waiting for Third Party"),
            ("resolved", "Resolved"),
            ("closed", "Closed"),
        ],
        "SupportCaseCategory": [
            ("billing", "Billing"),
            ("technical", "Technical Issue"),
            ("account", "Account Access"),
            ("feature", "Feature Request"),
            ("bug", "Bug Report"),
            ("security", "Security Concern"),
            ("compliance", "Compliance"),
            ("other", "Other"),
        ],
    },
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            "SupabaseBearer": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "Supabase access token.",
            }
        }
    },
}
HEALTHCHECK_EXTERNAL_DEPENDENCIES = env_bool("HEALTHCHECK_EXTERNAL_DEPENDENCIES", False)

SENTRY_DSN = env("SENTRY_DSN")
SENTRY_ENVIRONMENT = env("SENTRY_ENVIRONMENT")
SENTRY_RELEASE = env("SENTRY_RELEASE", "jt-code-api@0.1.0")
SENTRY_TRACES_SAMPLE_RATE = env_float("SENTRY_TRACES_SAMPLE_RATE", 0.1)
SENTRY_PROFILES_SAMPLE_RATE = env_float("SENTRY_PROFILES_SAMPLE_RATE", 0.0)
if SENTRY_DSN:
    from apps.core.sentry import init_sentry

    init_sentry(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        release=SENTRY_RELEASE,
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        profiles_sample_rate=SENTRY_PROFILES_SAMPLE_RATE,
    )

# Observability (Phase 15): Prometheus metrics and OpenTelemetry tracing.
# ``/metrics`` requires ``Authorization: Bearer <METRICS_AUTH_TOKEN>``.
METRICS_AUTH_TOKEN = env("METRICS_AUTH_TOKEN")
METRICS_DATABASE_STATE = env_bool("METRICS_DATABASE_STATE", True)
METRICS_STATE_CACHE_SECONDS = int(env("METRICS_STATE_CACHE_SECONDS", "15"))
# Celery workers serve their own exposition on this port (0 disables).
CELERY_METRICS_PORT = int(env("CELERY_METRICS_PORT", "0"))
OTEL_EXPORTER_OTLP_ENDPOINT = env("OTEL_EXPORTER_OTLP_ENDPOINT")
OTEL_EXPORTER_OTLP_HEADERS = env("OTEL_EXPORTER_OTLP_HEADERS")
OTEL_SERVICE_NAME = env("OTEL_SERVICE_NAME", "jt-code-api")
OTEL_SERVICE_VERSION = SENTRY_RELEASE
OTEL_ENVIRONMENT = env("OTEL_ENVIRONMENT", SENTRY_ENVIRONMENT or "development")
OTEL_TRACES_SAMPLE_RATIO = env_float("OTEL_TRACES_SAMPLE_RATIO", 0.1)

# Cost anomaly detection (Phase 19): last hour's provider cost against the trailing baseline.
USAGE_ANOMALY_BASELINE_DAYS = int(env("USAGE_ANOMALY_BASELINE_DAYS", "14"))
USAGE_ANOMALY_Z = env_float("USAGE_ANOMALY_Z", 4.0)
USAGE_ANOMALY_MIN_USD = env_float("USAGE_ANOMALY_MIN_USD", 5.0)
# Planned database failover/restore: reject writes with 503 + Retry-After, keep serving reads.
READ_ONLY_MODE = env_bool("READ_ONLY_MODE", False)
# Kafka partitions per topic for hot topics, e.g. "chat.request.accepted=12,jobs.job.created=12".
KAFKA_TOPIC_PARTITIONS_OVERRIDES = {
    name.strip(): int(count)
    for item in env_list("KAFKA_TOPIC_PARTITIONS_OVERRIDES")
    for name, _, count in [item.partition("=")]
    if name.strip() and count.strip().isdigit()
}

# Edge security (Phase 15). See apps/core/edge.py and infra/terraform/modules/cloudflare_edge.
TRUSTED_PROXY_HOPS = int(env("TRUSTED_PROXY_HOPS", "0"))
CLOUDFLARE_ORIGIN_SECRET = env("CLOUDFLARE_ORIGIN_SECRET")
CLOUDFLARE_ENFORCE_ORIGIN = env_bool("CLOUDFLARE_ENFORCE_ORIGIN", False)
CSP_API_POLICY = env(
    "CSP_API_POLICY", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
CSP_HTML_POLICY = env(
    "CSP_HTML_POLICY",
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://cdn.jsdelivr.net; "
    "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
)
PERMISSIONS_POLICY = env(
    "PERMISSIONS_POLICY",
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), "
    "payment=(), usb=(), browsing-topics=()",
)
CROSS_ORIGIN_RESOURCE_POLICY = env("CROSS_ORIGIN_RESOURCE_POLICY", "same-site")
# Signed machine-to-machine webhooks (n8n, relays): timestamp window and nonce TTL.
WEBHOOK_REPLAY_TOLERANCE_SECONDS = int(env("WEBHOOK_REPLAY_TOLERANCE_SECONDS", "300"))

# Audit pipeline (Phase 15): mutating requests to these route templates are
# audited, successful or denied - (route regex, category, severity, methods).
AUDIT_ROUTE_RULES: tuple[tuple[str, str, str, str], ...] = (
    (r"^api/v1/(api-keys|webhooks|connector-accounts|kafka-consumers)/", "configuration", "medium", "*"),
    (r"^api/v1/integrations/", "configuration", "medium", "*"),
    (r"^api/v1/(tool-policies|tool-credentials|mcp/servers)/", "security", "high", "*"),
    (r"^api/v1/tool-approvals/", "authorization", "medium", "*"),
    (r"^api/v1/(plans/.+/subscribe|subscriptions|wallets|payment-methods)/", "billing", "medium", "*"),
    (r"^api/v1/billing/", "billing", "medium", "*"),
    (r"^api/v1/(organizations|settings/organization|settings/account|settings/export)", "admin", "high", "*"),
    (r"^api/v1/accounts/me/password/", "security", "high", "*"),
    (r"^api/v1/(settings/consents|consents|retention-rules)/", "configuration", "medium", "*"),
    (r"^api/v1/(files|knowledge)/", "file_operation", "low", "DELETE"),
    (r"^api/v1/files/.+/(restore|access)/", "file_operation", "low", "*"),
    (r"^api/v1/(n8n/workflows|automations)", "configuration", "medium", "*"),
)

if os.getenv("DJANGO_SETTINGS_MODULE") == "config.settings.base":
    raise ImproperlyConfigured("config.settings.base is shared settings, not a deployable profile.")
CHAT_SSE_POLL_SECONDS = float(env("CHAT_SSE_POLL_SECONDS", "1"))
