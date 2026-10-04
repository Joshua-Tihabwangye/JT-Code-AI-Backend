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
    "N8N_DISPATCH_SECRET",
    "N8N_CALLBACK_BASE_URL",
    "STRIPE_SECRET_KEY",
    "WEBHOOK_ALLOWED_HOSTS",
    "WEBHOOK_SIGNING_SECRET",
    "STRIPE_WEBHOOK_SECRET",
    "SENTRY_DSN",
    "SENTRY_ENVIRONMENT",
    "METRICS_AUTH_TOKEN",
)
_SECRET_ENV = (
    "DJANGO_SECRET_KEY",
    "SUPABASE_WEBHOOK_SIGNING_SECRET",
    "SUPABASE_SECRET_KEY",
    "N8N_SENTRY_RELAY_SECRET",
    "N8N_API_KEY",
    "N8N_WEBHOOK_SECRET",
    "N8N_DISPATCH_SECRET",
    "KAFKA_SASL_PASSWORD",
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "WEBHOOK_SIGNING_SECRET",
    "IMAGEKIT_PRIVATE_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "LLAMA_API_KEY",
    "SENTRY_DSN",
    "METRICS_AUTH_TOKEN",
    "CLOUDFLARE_ORIGIN_SECRET",
)
_BOOL_ENV = (
    "DJANGO_DEBUG",
    "AI_GATEWAY_FALLBACK_ENABLED",
    "HEALTHCHECK_EXTERNAL_DEPENDENCIES",
    "SUPABASE_ALLOW_ANONYMOUS_USERS",
    "BROWSER_TOOL_ENABLED",
    "ENABLE_MCP",
    "ASSET_LOCAL_FALLBACK_ENABLED",
    "METRICS_DATABASE_STATE",
    "CLOUDFLARE_ENFORCE_ORIGIN",
)
_INT_ENV = (
    "AGENT_MAX_ITERATIONS",
    "AI_GATEWAY_MAX_LATENCY_MS",
    "IMAGEKIT_MAX_UPLOAD_BYTES",
    "IMAGEKIT_UPLOAD_AUTH_TTL_SECONDS",
    "ASSET_DELETE_GRACE_DAYS",
    "ASSET_ORPHAN_GRACE_HOURS",
    "IMAGEKIT_RECONCILE_PAGE_SIZE",
    "IMAGEKIT_RECONCILE_MAX_PAGES",
    "IMAGEKIT_RECONCILE_BATCH_SIZE",
    "IMAGEKIT_RECONCILE_INTERVAL_HOURS",
    "IMAGEKIT_RECONCILE_MAX_DEPTH",
    "ASSET_DELETE_MAX_ATTEMPTS",
    "DATABASE_CONN_MAX_AGE",
    "DATABASE_CONNECT_TIMEOUT_SECONDS",
    "VECTOR_EMBEDDING_DIMENSIONS",
    "RAG_CHUNK_SIZE",
    "RAG_CHUNK_OVERLAP",
    "RAG_TOP_K",
    "RAG_RERANK_TOP_K",
    "RAG_MAX_EXTRACTED_BYTES",
    "RAG_EMBEDDING_MAX_RETRIES",
    "RAG_EMBEDDING_BATCH_SIZE",
    "RAG_HYBRID_CANDIDATES",
    "RAG_MAX_CONTEXT_TOKENS",
    "RAG_INGESTION_STALLED_MINUTES",
    "RAG_INGESTION_MAX_RETRIES",
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
    "MAX_CONCURRENT_JOBS_PER_TENANT",
    "MAX_CONCURRENT_CHAT_REQUESTS_PER_TENANT",
    "MAX_CONCURRENT_ANALYSIS_RUNS_PER_TENANT",
    "USAGE_RESERVATION_TTL_MINUTES",
    "THROTTLE_TENANT_MULTIPLIER",
    "STRIPE_WEBHOOK_TOLERANCE_SECONDS",
    "STRIPE_EVENT_MAX_ATTEMPTS",
    "BILLING_TOPUP_MIN_CENTS",
    "BILLING_TOPUP_MAX_CENTS",
    "BILLING_AUTO_TOPUP_COOLDOWN_MINUTES",
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
    "ANALYTICS_MAX_INLINE_BYTES",
    "ANALYTICS_MAX_DATASET_BYTES",
    "ANALYTICS_MAX_RESULT_BYTES",
    "ANALYTICS_MAX_DATASET_ROWS",
    "ANALYTICS_MAX_DATASET_COLUMNS",
    "ANALYTICS_MAX_DATASET_CELLS",
    "ANALYTICS_RESULT_PREVIEW_ROWS",
    "ANALYTICS_MAX_CHART_POINTS",
    "ANALYTICS_MAX_PLOTLY_SPEC_BYTES",
    "ANALYTICS_DOWNLOAD_TIMEOUT_SECONDS",
    "ANALYTICS_TASK_SOFT_TIME_LIMIT_SECONDS",
    "ANALYTICS_TASK_TIME_LIMIT_SECONDS",
    "ANALYTICS_STALLED_AFTER_MINUTES",
    "ANALYTICS_SANDBOX_MEMORY_MB",
    "ANALYTICS_SANDBOX_CPU_SECONDS",
    "ANALYTICS_SANDBOX_TIMEOUT_SECONDS",
    "ANALYTICS_INLINE_SPEC_BYTES",
    "METRICS_STATE_CACHE_SECONDS",
    "CELERY_METRICS_PORT",
    "TRUSTED_PROXY_HOPS",
    "WEBHOOK_REPLAY_TOLERANCE_SECONDS",
    "N8N_REQUEST_TIMEOUT_SECONDS",
    "N8N_RETRY_BASE_SECONDS",
    "N8N_RETRY_MAX_SECONDS",
)
_FLOAT_ENV = (
    "AI_GATEWAY_MAX_COST_USD",
    "BILLING_CREDIT_VALUE_USD",
    "BILLING_FX_BUFFER",
    "BILLING_MARGIN_MULTIPLIER",
    "USAGE_RECONCILIATION_DRIFT_RATIO",
    "VECTOR_MIN_SIMILARITY",
    "RAG_EVAL_MIN_RECALL",
    "RAG_EVAL_MIN_MRR",
    "RAG_URL_FETCH_TIMEOUT_SECONDS",
    "RAG_EMBEDDING_TIMEOUT_SECONDS",
    "IMAGEKIT_API_TIMEOUT_SECONDS",
    "SENTRY_TRACES_SAMPLE_RATE",
    "SENTRY_PROFILES_SAMPLE_RATE",
    "CHAT_SSE_HEARTBEAT_SECONDS",
    "AI_RETRY_BASE_SECONDS",
    "AGENT_MAX_COST_USD",
    "AI_RETRY_MAX_BACKOFF_SECONDS",
    "CHAT_SSE_POLL_SECONDS",
    "CHAT_SSE_RECONCILIATION_SECONDS",
    "WEBHOOK_DELIVERY_TIMEOUT_SECONDS",
    "OTEL_TRACES_SAMPLE_RATIO",
)
_FRACTION_ENV = {
    "VECTOR_MIN_SIMILARITY": (0.0, 1.0),
    "RAG_EVAL_MIN_RECALL": (0.0, 1.0),
    "RAG_EVAL_MIN_MRR": (0.0, 1.0),
    "SENTRY_TRACES_SAMPLE_RATE": (0.0, 1.0),
    "SENTRY_PROFILES_SAMPLE_RATE": (0.0, 1.0),
    "OTEL_TRACES_SAMPLE_RATIO": (0.0, 1.0),
}
_THROTTLE_ENV = (
    "THROTTLE_CHAT",
    "THROTTLE_IMAGES",
    "THROTTLE_EMBEDDINGS",
    "THROTTLE_CONVERSIONS",
    "THROTTLE_RESEARCH",
    "THROTTLE_BURST",
    "THROTTLE_AGENT_RUNS",
    "THROTTLE_ANALYTICS",
    "THROTTLE_IP",
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


def _check_analytics(problems: list[str]) -> None:
    names = [name for name in _INT_ENV if name.startswith("ANALYTICS_")]
    for name in names:
        value = _val(name)
        if value and value.isdigit() and int(value) < 1:
            problems.append(f"{name} must be at least 1.")
    soft = _val("ANALYTICS_TASK_SOFT_TIME_LIMIT_SECONDS", "270")
    hard = _val("ANALYTICS_TASK_TIME_LIMIT_SECONDS", "300")
    if soft.isdigit() and hard.isdigit() and int(soft) >= int(hard):
        problems.append("ANALYTICS_TASK_SOFT_TIME_LIMIT_SECONDS must be below the hard time limit.")
    stalled = _val("ANALYTICS_STALLED_AFTER_MINUTES", "15")
    if stalled.isdigit() and hard.isdigit() and int(stalled) * 60 <= int(hard):
        problems.append("ANALYTICS_STALLED_AFTER_MINUTES must exceed the hard task time limit.")
    sandbox = _val("ANALYTICS_SANDBOX_TIMEOUT_SECONDS", "250")
    if sandbox.isdigit() and soft.isdigit() and int(sandbox) >= int(soft):
        problems.append("ANALYTICS_SANDBOX_TIMEOUT_SECONDS must be below the soft task time limit.")


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


def _check_billing(problems: list[str], *, strict: bool) -> None:
    low, high = _val("BILLING_TOPUP_MIN_CENTS", "500"), _val("BILLING_TOPUP_MAX_CENTS", "100000")
    if low.isdigit() and high.isdigit() and int(low) >= int(high):
        problems.append("BILLING_TOPUP_MIN_CENTS must be below BILLING_TOPUP_MAX_CENTS.")
    if not strict:
        return
    frontend = _val("FRONTEND_URL")
    if not frontend.startswith("https://"):
        problems.append("FRONTEND_URL must be an https:// origin in staging/production.")
    if not _val("STRIPE_SECRET_KEY").startswith(("sk_", "rk_")):
        problems.append("STRIPE_SECRET_KEY must be a Stripe secret (sk_) or restricted (rk_) key.")
    if not _val("STRIPE_WEBHOOK_SECRET").startswith("whsec_"):
        problems.append("STRIPE_WEBHOOK_SECRET must be a Stripe webhook signing secret (whsec_).")


def _check_observability(problems: list[str], *, strict: bool) -> None:
    tolerance = _val("WEBHOOK_REPLAY_TOLERANCE_SECONDS", "300")
    if tolerance.isdigit() and not 30 <= int(tolerance) <= 900:
        problems.append("WEBHOOK_REPLAY_TOLERANCE_SECONDS must be between 30 and 900.")
    hops = _val("TRUSTED_PROXY_HOPS", "0")
    if hops.isdigit() and int(hops) > 5:
        problems.append("TRUSTED_PROXY_HOPS must be the number of proxies you operate (0-5).")
    enforce = _val("CLOUDFLARE_ENFORCE_ORIGIN", "false").lower() in {"1", "true", "yes", "on"}
    if enforce and len(_val("CLOUDFLARE_ORIGIN_SECRET")) < 32:
        problems.append("CLOUDFLARE_ENFORCE_ORIGIN requires CLOUDFLARE_ORIGIN_SECRET (32+ characters).")
    if not strict:
        return
    if (token := _val("METRICS_AUTH_TOKEN")) and len(token) < 32:
        problems.append("METRICS_AUTH_TOKEN must be at least 32 characters.")
    if (endpoint := _val("OTEL_EXPORTER_OTLP_ENDPOINT")) and urlparse(endpoint).scheme != "https":
        problems.append("OTEL_EXPORTER_OTLP_ENDPOINT must use HTTPS in deployable environments.")


def _check_n8n(problems: list[str], *, strict: bool) -> None:
    base, ceiling = _val("N8N_RETRY_BASE_SECONDS", "30"), _val("N8N_RETRY_MAX_SECONDS", "900")
    if base.isdigit() and ceiling.isdigit() and not 0 < int(base) <= int(ceiling):
        problems.append("N8N_RETRY_BASE_SECONDS must be positive and at most N8N_RETRY_MAX_SECONDS.")
    dispatch, callback = _val("N8N_DISPATCH_SECRET"), _val("N8N_WEBHOOK_SECRET")
    if dispatch and dispatch == callback:
        problems.append("N8N_DISPATCH_SECRET and N8N_WEBHOOK_SECRET must differ (one per direction).")
    if not strict:
        return
    for name in ("N8N_DISPATCH_SECRET", "N8N_WEBHOOK_SECRET"):
        if (value := _val(name)) and len(value) < 32:
            problems.append(f"{name} must be at least 32 characters.")
    for name in ("N8N_WEBHOOK_BASE_URL", "N8N_CALLBACK_BASE_URL"):
        if (value := _val(name)) and urlparse(value).scheme != "https":
            problems.append(f"{name} must use HTTPS in deployable environments.")


def _check_rag(problems: list[str], *, strict: bool) -> None:
    provider = _val("RAG_EMBEDDING_PROVIDER", "gemini").lower()
    if provider not in {"openai", "gemini", "echo"}:
        problems.append("RAG_EMBEDDING_PROVIDER must be openai, gemini, or echo.")
        return
    for name in ("RAG_RERANKER", "RAG_JUDGE"):
        if _val(name, "model").lower() not in {"model", "deterministic"}:
            problems.append(f"{name} must be model or deterministic.")
    size = _val("RAG_CHUNK_SIZE", "1000")
    overlap = _val("RAG_CHUNK_OVERLAP", "200")
    if size.isdigit() and overlap.isdigit() and int(overlap) >= int(size):
        problems.append("RAG_CHUNK_OVERLAP must be smaller than RAG_CHUNK_SIZE.")
    if not strict:
        return
    if provider == "echo":
        problems.append("RAG_EMBEDDING_PROVIDER=echo is not allowed in staging/production.")
    if provider == "openai" and not _val("OPENAI_API_KEY"):
        problems.append("OPENAI_API_KEY is required when RAG_EMBEDDING_PROVIDER=openai.")
    if provider == "gemini" and not _val("GEMINI_API_KEY"):
        problems.append("GEMINI_API_KEY is required when RAG_EMBEDDING_PROVIDER=gemini.")


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
    _check_rag(problems, strict=strict)
    _check_agents(problems)
    _check_analytics(problems)
    _check_billing(problems, strict=strict)
    _check_tools(problems, strict=strict)
    _check_observability(problems, strict=strict)
    _check_n8n(problems, strict=strict)
    if not strict:
        return problems

    for name in _REQUIRED_STRICT:
        if not _val(name):
            problems.append(f"Missing required environment variable: {name}.")
    for name in _SECRET_ENV:
        _check_secret(problems, name)

    if _val("DJANGO_DEBUG", "0").lower() in {"1", "true", "yes", "on"}:
        problems.append("DJANGO_DEBUG must not be enabled in staging or production.")
    if _val("ASSET_LOCAL_FALLBACK_ENABLED", "false").lower() in {"1", "true", "yes", "on"}:
        problems.append("ASSET_LOCAL_FALLBACK_ENABLED is not allowed in staging/production.")
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
