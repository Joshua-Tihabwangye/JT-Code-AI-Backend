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
    "SUPABASE_JWT_SECRET",
    "SUPABASE_JWT_ISSUER",
    "SUPABASE_JWT_AUDIENCE",
    "SUPABASE_WEBHOOK_SIGNING_SECRET",
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
    "STRIPE_WEBHOOK_SECRET",
    "SENTRY_DSN",
    "SENTRY_ENVIRONMENT",
)
_SECRET_ENV = (
    "DJANGO_SECRET_KEY",
    "SUPABASE_JWT_SECRET",
    "SUPABASE_WEBHOOK_SIGNING_SECRET",
    "N8N_SENTRY_RELAY_SECRET",
    "N8N_API_KEY",
    "N8N_WEBHOOK_SECRET",
    "KAFKA_SASL_PASSWORD",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "IMAGEKIT_PRIVATE_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "SENTRY_DSN",
)
_BOOL_ENV = (
    "DJANGO_DEBUG",
    "AI_GATEWAY_FALLBACK_ENABLED",
    "PGVECTOR_ENABLED",
    "HEALTHCHECK_EXTERNAL_DEPENDENCIES",
)
_INT_ENV = (
    "AGENT_MAX_ITERATIONS",
    "AI_GATEWAY_MAX_LATENCY_MS",
    "IMAGEKIT_MAX_UPLOAD_BYTES",
    "IMAGEKIT_UPLOAD_AUTH_TTL_SECONDS",
    "DATABASE_CONN_MAX_AGE",
    "VECTOR_EMBEDDING_DIMENSIONS",
    "RAG_CHUNK_SIZE",
    "RAG_CHUNK_OVERLAP",
    "RAG_TOP_K",
    "RAG_RERANK_TOP_K",
    "RAG_MAX_EXTRACTED_BYTES",
    "AUDIT_EVENT_RETENTION_DAYS",
    "SAFETY_EVENT_RETENTION_DAYS",
    "WEBHOOK_MAX_RETRIES",
    "WEBHOOK_RETRY_BASE_DELAY",
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
    for name in _URL_LIST_ENV:
        _check_origins(problems, name, require_https=strict)
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

    database_url = _val("DATABASE_URL")
    database = urlparse(database_url)
    if database_url and (database.scheme not in {"postgres", "postgresql"} or not database.hostname):
        problems.append("DATABASE_URL must be a PostgreSQL connection string.")
    sslmode = parse_qs(database.query).get("sslmode", [""])[0].lower()
    if database_url and sslmode not in {"require", "verify-ca", "verify-full"}:
        problems.append(
            "DATABASE_URL must set sslmode to require, verify-ca, or verify-full in deployable environments."
        )

    for name in ("REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"):
        _check_tls_url(problems, name)
    protocol = _val("KAFKA_SECURITY_PROTOCOL").upper()
    if protocol != "SASL_SSL":
        problems.append("KAFKA_SECURITY_PROTOCOL must be SASL_SSL in deployable environments.")
    supabase_url, issuer = _val("SUPABASE_URL"), _val("SUPABASE_JWT_ISSUER")
    if supabase_url and issuer and not issuer.startswith(supabase_url.rstrip("/") + "/"):
        problems.append("SUPABASE_JWT_ISSUER must start with SUPABASE_URL.")
    for name in ("SUPABASE_URL", "IMAGEKIT_ENDPOINT_URL", "N8N_BASE_URL"):
        value = _val(name)
        if value and urlparse(value).scheme != "https":
            problems.append(f"{name} must use HTTPS in deployable environments.")
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
