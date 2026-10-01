"""Fail-closed, typed configuration validation for deployable settings profiles."""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse

_RATE_RE = re.compile(r"^\d+/(second|minute|hour|day)$")
_PROFILES = ("development", "test", "staging", "production")
_DEPLOYMENT_PROFILES = frozenset({"staging", "production"})
_PLACEHOLDER_SECRETS = (
    "unsafe-local-development-key-change-me",
    "replace_me",
    "replace-with-at-least-50-random-characters",
    "your-supabase-jwt-secret",
    "your-api-key",
    "placeholder",
)
_REQUIRED_STRICT = (
    "DJANGO_SECRET_KEY",
    "DJANGO_ALLOWED_HOSTS",
    "CORS_ALLOWED_ORIGINS",
    "CSRF_TRUSTED_ORIGINS",
    "DATABASE_URL",
    "SUPABASE_URL",
    "SUPABASE_JWKS_URL",
    "SUPABASE_JWT_ISSUER",
    "SUPABASE_JWT_AUDIENCE",
    "SUPABASE_WEBHOOK_SIGNING_SECRET",
    "SUPABASE_SECRET_KEY",
    "REDIS_URL",
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
    "KAFKA_BOOTSTRAP_SERVERS",
    "KAFKA_SECURITY_PROTOCOL",
    "KAFKA_SASL_MECHANISM",
    "KAFKA_SASL_USERNAME",
    "KAFKA_SASL_PASSWORD",
    "IMAGEKIT_PUBLIC_KEY",
    "IMAGEKIT_PRIVATE_KEY",
    "IMAGEKIT_ENDPOINT_URL",
    "N8N_BASE_URL",
    "N8N_API_KEY",
    "N8N_WEBHOOK_SECRET",
    "N8N_SENTRY_RELAY_SECRET",
    "STRIPE_SECRET_KEY",
    "WEBHOOK_ALLOWED_HOSTS",
    "WEBHOOK_SIGNING_SECRET",
    "STRIPE_WEBHOOK_SECRET",
    "SENTRY_DSN",
    "SENTRY_ENVIRONMENT",
)
_SECRET_ENV = (
    "DJANGO_SECRET_KEY",
    "SUPABASE_WEBHOOK_SIGNING_SECRET",
    "SUPABASE_SECRET_KEY",
    "N8N_SENTRY_RELAY_SECRET",
    "N8N_API_KEY",
    "N8N_WEBHOOK_SECRET",
    "KAFKA_SASL_PASSWORD",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "WEBHOOK_SIGNING_SECRET",
    "IMAGEKIT_PRIVATE_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "LLAMA_API_KEY",
    "SENTRY_DSN",
)
_BOOL_ENV = (
    "DJANGO_DEBUG",
    "AI_GATEWAY_FALLBACK_ENABLED",
    "PGVECTOR_ENABLED",
    "HEALTHCHECK_EXTERNAL_DEPENDENCIES",
    "SUPABASE_ALLOW_ANONYMOUS_USERS",
    "BROWSER_TOOL_ENABLED",
    "ENABLE_MCP",
)
_INT_ENV = (
    "AGENT_MAX_ITERATIONS",
    "AI_GATEWAY_MAX_LATENCY_MS",
    "IMAGEKIT_MAX_UPLOAD_BYTES",
    "IMAGEKIT_UPLOAD_AUTH_TTL_SECONDS",
    "DATABASE_CONN_MAX_AGE",
    "DATABASE_CONNECT_TIMEOUT_SECONDS",
    "VECTOR_EMBEDDING_DIMENSIONS",
    "RAG_CHUNK_SIZE",
    "RAG_CHUNK_OVERLAP",
    "RAG_TOP_K",
    "RAG_RERANK_TOP_K",
    "RAG_MAX_EXTRACTED_BYTES",
    "EVENT_OUTBOX_MAX_ATTEMPTS",
    "EVENT_OUTBOX_MAX_BACKOFF_SECONDS",
    "EVENT_OUTBOX_LEASE_SECONDS",
    "AUDIT_EVENT_RETENTION_DAYS",
    "SAFETY_EVENT_RETENTION_DAYS",
    "WEBHOOK_MAX_RETRIES",
    "WEBHOOK_RETRY_BASE_DELAY",
    "WEBHOOK_RETRY_MAX_SECONDS",
    "CHAT_SSE_MAX_SECONDS",
    "CHAT_DISPATCH_GRACE_SECONDS",
    "AI_REQUEST_TIMEOUT_SECONDS",
    "LANGGRAPH_MAX_STEPS",
    "AGENT_MAX_TOOL_CALLS",
    "EXTERNAL_API_TIMEOUT_SECONDS",
    "TOOL_MAX_RESPONSE_BYTES",
    "TOOL_MAX_OUTPUT_CHARS",
    "TOOL_MAX_ARGUMENT_BYTES",
    "TOOL_APPROVAL_TTL_SECONDS",
    "MCP_TOOL_TIMEOUT_SECONDS",
    "AGENT_MAX_DURATION_SECONDS",
    "AGENT_RUN_STALLED_TIMEOUT_SECONDS",
    "AGENT_RUN_MAX_ATTEMPTS",
    "MAX_CONCURRENT_AGENT_RUNS_PER_TENANT",
    "AI_MAX_RETRIES",
    "AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS",
    "CHAT_REQUEST_STALLED_TIMEOUT_SECONDS",
    "CHAT_MAX_CONTEXT_MESSAGES",
    "JOB_STALLED_TIMEOUT_SECONDS",
    "EVENT_OUTBOX_RETENTION_DAYS",
    "KAFKA_CONSUMER_MAX_ATTEMPTS",
    "KAFKA_CONSUMER_RETRY_MAX_SECONDS",
    "KAFKA_TOPIC_PARTITIONS",
    "KAFKA_TOPIC_REPLICATION_FACTOR",
    "KAFKA_TOPIC_RETENTION_MS",
)
_FLOAT_ENV = (
    "AI_GATEWAY_MAX_COST_USD",
    "BILLING_CREDIT_VALUE_USD",
    "BILLING_FX_BUFFER",
    "BILLING_MARGIN_MULTIPLIER",
    "VECTOR_MIN_SIMILARITY",
    "RAG_SIMILARITY_THRESHOLD",
    "RAG_URL_FETCH_TIMEOUT_SECONDS",
    "SENTRY_TRACES_SAMPLE_RATE",
    "SENTRY_PROFILES_SAMPLE_RATE",
    "CHAT_SSE_HEARTBEAT_SECONDS",
    "AI_RETRY_BASE_SECONDS",
    "AGENT_MAX_COST_USD",
    "AI_RETRY_MAX_BACKOFF_SECONDS",
    "CHAT_SSE_POLL_SECONDS",
    "CHAT_SSE_RECONCILIATION_SECONDS",
    "WEBHOOK_DELIVERY_TIMEOUT_SECONDS",
)
_FRACTION_ENV = {
    "VECTOR_MIN_SIMILARITY": (0.0, 1.0),
    "RAG_SIMILARITY_THRESHOLD": (0.0, 1.0),
    "SENTRY_TRACES_SAMPLE_RATE": (0.0, 1.0),
    "SENTRY_PROFILES_SAMPLE_RATE": (0.0, 1.0),
}
_THROTTLE_ENV = (
    "THROTTLE_CHAT",
    "THROTTLE_IMAGES",
    "THROTTLE_EMBEDDINGS",
    "THROTTLE_CONVERSIONS",
    "THROTTLE_RESEARCH",
    "THROTTLE_BURST",
    "THROTTLE_AGENT_RUNS",
)
_URL_LIST_ENV = ("CORS_ALLOWED_ORIGINS", "CSRF_TRUSTED_ORIGINS")


def _val(name: str, default: str | None = None) -> str:
    return os.getenv(name, default) or ""


def _check_int(problems: list[str], name: str) -> None:
    value = _val(name)
    if not value:
        return
    try:
        int(value)
    except ValueError:
        problems.append(f"{name} must be an integer, got {value!r}.")


def _check_float(problems: list[str], name: str) -> None:
    value = _val(name)
    if not value:
        return
    try:
        number = float(value)
    except ValueError:
        problems.append(f"{name} must be a number, got {value!r}.")
        return
    if (bounds := _FRACTION_ENV.get(name)) and not bounds[0] <= number <= bounds[1]:
        problems.append(f"{name} must be within [{bounds[0]}, {bounds[1]}], got {value!r}.")


def _check_bool(problems: list[str], name: str) -> None:
    value = _val(name)
    if value and value.lower() not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
        problems.append(f"{name} must be a boolean value, got {value!r}.")


def _origins(value: str) -> list[str]:
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def _check_origins(problems: list[str], name: str, *, require_https: bool) -> None:
    for origin in _origins(_val(name)):
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
            problems.append(f"{name} entry {origin!r} is not a valid origin URL.")
        elif require_https and parsed.scheme != "https":
            problems.append(f"{name} entry {origin!r} must use HTTPS in deployable environments.")


def _check_secret(problems: list[str], name: str) -> None:
    value = _val(name)
    if value and any(marker in value.lower() for marker in _PLACEHOLDER_SECRETS):
        problems.append(f"{name} still contains a placeholder value; refusing to start.")
    if name == "DJANGO_SECRET_KEY" and value and (len(value) < 50 or len(set(value)) < 5):
        problems.append(
            "DJANGO_SECRET_KEY must be at least 50 characters with at least five distinct characters."
        )


def _check_tls_url(problems: list[str], name: str) -> None:
    value = _val(name)
    if not value:
        return
    parsed = urlparse(value)
    if parsed.scheme != "rediss" or not parsed.hostname:
        problems.append(f"{name} must use a TLS redis URL (rediss://) in deployable environments.")


_GEMINI_THRESHOLDS = frozenset(
    {"BLOCK_NONE", "BLOCK_ONLY_HIGH", "BLOCK_MEDIUM_AND_ABOVE", "BLOCK_LOW_AND_ABOVE", "OFF"}
)


def _check_tools(problems: list[str], *, strict: bool) -> None:
    for key in [item.strip() for item in _val("TOOL_CREDENTIALS_ENCRYPTION_KEYS").split(",") if item.strip()]:
        try:
            import base64

            if len(base64.urlsafe_b64decode(key.encode())) != 32:
                raise ValueError
        except ValueError:
            problems.append(
                "TOOL_CREDENTIALS_ENCRYPTION_KEYS must contain Fernet keys (32-byte url-safe base64)."
            )
            break
    for name in ("GITHUB_API_BASE", "SLACK_API_BASE", "SEARCH_API_BASE"):
        if (value := _val(name)) and urlparse(value).scheme != "https":
            problems.append(f"{name} must use HTTPS.")
    prefix = _val("TOOL_GITHUB_BRANCH_PREFIX", "jt-code/")
    if not prefix or prefix.rstrip("/").lower() in {"main", "master", ""}:
        problems.append("TOOL_GITHUB_BRANCH_PREFIX must be a dedicated branch namespace such as 'jt-code/'.")
    if strict and not _val("TOOL_CREDENTIALS_ENCRYPTION_KEYS"):
        problems.append("TOOL_CREDENTIALS_ENCRYPTION_KEYS is required in staging/production.")


def _check_agents(problems: list[str]) -> None:
    mode = _val("AGENT_ROUTER_MODE")
    if mode and mode not in {"rules", "model"}:
        problems.append("AGENT_ROUTER_MODE must be 'rules' or 'model'.")


def _check_ai_gateway(problems: list[str], *, strict: bool) -> None:
    threshold = _val("GEMINI_SAFETY_THRESHOLD")
    if threshold and threshold not in _GEMINI_THRESHOLDS:
        problems.append(f"GEMINI_SAFETY_THRESHOLD must be one of {sorted(_GEMINI_THRESHOLDS)}.")
    llama_base = _val("LLAMA_API_BASE")
    if strict and llama_base and urlparse(llama_base).scheme != "https":
        problems.append("LLAMA_API_BASE must use HTTPS in deployable environments.")
    if not strict:
        return
    if _val("AI_PROVIDER").lower() == "echo":
        problems.append("AI_PROVIDER=echo is a development stub and is not allowed in staging/production.")
    if not _val("GEMINI_API_KEY") and not (_val("LLAMA_API_KEY") and llama_base):
        problems.append(
            "Configure GEMINI_API_KEY or LLAMA_API_KEY (with LLAMA_API_BASE); the AI gateway has no provider."
        )
    if _val("GEMINI_SAFETY_THRESHOLD") in {"BLOCK_NONE", "OFF"}:
        problems.append("GEMINI_SAFETY_THRESHOLD must not disable Gemini safety filtering in production.")


def validate_environment(profile: str) -> list[str]:
    """Return all invalid configuration conditions for ``profile`` without leaking values."""
    if profile not in _PROFILES:
        raise ValueError(f"Unknown profile {profile!r}. Valid: {list(_PROFILES)}")

    problems: list[str] = []
    strict = profile in _DEPLOYMENT_PROFILES
    for name in _INT_ENV:
        _check_int(problems, name)
    for name in _FLOAT_ENV:
        _check_float(problems, name)
    for name in _BOOL_ENV:
        _check_bool(problems, name)
    for name in _THROTTLE_ENV:
        if (value := _val(name)) and not _RATE_RE.fullmatch(value):
            problems.append(f"{name} does not match <count>/<period>, got {value!r}.")
    for name in (
        "EVENT_OUTBOX_MAX_ATTEMPTS",
        "EVENT_OUTBOX_MAX_BACKOFF_SECONDS",
        "EVENT_OUTBOX_LEASE_SECONDS",
    ):
        if (value := _val(name)) and value.isdigit() and int(value) < 1:
            problems.append(f"{name} must be at least 1.")
    for name in _URL_LIST_ENV:
        _check_origins(problems, name, require_https=strict)
    _check_ai_gateway(problems, strict=strict)
    _check_agents(problems)
    _check_tools(problems, strict=strict)
    if not strict:
        return problems

    for name in _REQUIRED_STRICT:
        if not _val(name):
            problems.append(f"Missing required environment variable: {name}.")
    for name in _SECRET_ENV:
        _check_secret(problems, name)

    if _val("DJANGO_DEBUG", "0").lower() in {"1", "true", "yes", "on"}:
        problems.append("DJANGO_DEBUG must not be enabled in staging or production.")
    hosts = _origins(_val("DJANGO_ALLOWED_HOSTS"))
    if not hosts:
        problems.append("DJANGO_ALLOWED_HOSTS is required in production/staging.")
    elif any(host == "*" for host in hosts):
        problems.append('DJANGO_ALLOWED_HOSTS may not contain "*" in production/staging.')

    callback_hosts = _origins(_val("WEBHOOK_ALLOWED_HOSTS"))
    if not callback_hosts:
        problems.append("WEBHOOK_ALLOWED_HOSTS is required in production/staging.")
    elif any("://" in host or "/" in host or host == "*" for host in callback_hosts):
        problems.append("WEBHOOK_ALLOWED_HOSTS must contain DNS hostnames, not URLs or wildcard-all entries.")

    database_url = _val("DATABASE_URL")
    database = urlparse(database_url)
    if database_url and (database.scheme not in {"postgres", "postgresql"} or not database.hostname):
        problems.append("DATABASE_URL must be a PostgreSQL connection string.")
    sslmode = parse_qs(database.query).get("sslmode", [""])[0].lower()
    if database_url and sslmode not in {"require", "verify-ca", "verify-full"}:
        problems.append(
            "DATABASE_URL must set sslmode to require, verify-ca, or verify-full in deployable environments."
        )

    if _val("DATABASE_POOLER_MODE", "direct").lower() not in {"direct", "session", "transaction"}:
        problems.append("DATABASE_POOLER_MODE must be direct, session, or transaction.")
    pooler_mode = _val("DATABASE_POOLER_MODE", "direct").lower()
    if pooler_mode == "transaction" and _val("DATABASE_CONN_MAX_AGE", "60") != "0":
        problems.append("DATABASE_CONN_MAX_AGE must be 0 when DATABASE_POOLER_MODE=transaction.")

    for name in ("REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"):
        _check_tls_url(problems, name)
    protocol = _val("KAFKA_SECURITY_PROTOCOL").upper()
    if protocol != "SASL_SSL":
        problems.append("KAFKA_SECURITY_PROTOCOL must be SASL_SSL in deployable environments.")
    supabase_url, issuer = _val("SUPABASE_URL"), _val("SUPABASE_JWT_ISSUER")
    if supabase_url and issuer and not issuer.startswith(supabase_url.rstrip("/") + "/"):
        problems.append("SUPABASE_JWT_ISSUER must start with SUPABASE_URL.")
    jwks_url = _val("SUPABASE_JWKS_URL")
    expected_jwks_url = supabase_url.rstrip("/") + "/auth/v1/.well-known/jwks.json"
    if supabase_url and jwks_url != expected_jwks_url:
        problems.append("SUPABASE_JWKS_URL must be the JWKS endpoint for SUPABASE_URL.")
    for name in ("SUPABASE_URL", "SUPABASE_JWKS_URL", "IMAGEKIT_ENDPOINT_URL", "N8N_BASE_URL"):
        value = _val(name)
        if value and urlparse(value).scheme != "https":
            problems.append(f"{name} must use HTTPS in deployable environments.")
    secret_key = _val("SUPABASE_SECRET_KEY")
    if secret_key.startswith("sb_publishable_"):
        problems.append("SUPABASE_SECRET_KEY must be the server secret key, not the publishable key.")
    if _val("SUPABASE_JWT_SECRET"):
        problems.append(
            "SUPABASE_JWT_SECRET must be unset in deployable environments; tokens are verified with JWKS."
        )
    if _val("SENTRY_ENVIRONMENT").lower() in {"development", "dev", "test"}:
        problems.append("SENTRY_ENVIRONMENT must identify the deployable environment, not development/test.")
    return problems


def validate_settings(profile: str) -> None:
    from django.core.exceptions import ImproperlyConfigured

    if problems := validate_environment(profile):
        raise ImproperlyConfigured(
            f"Invalid configuration for profile {profile!r}:\n"
            + "\n".join(f"  - {item}" for item in problems)
        )
