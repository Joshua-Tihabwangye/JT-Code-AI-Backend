"""Strict, typed environment validation.

Phase 1 backlog: "Create strict environment validation using typed settings".

Every environment settings module calls :func:`validate_environment` after
``from .base import *``. Problems are aggregated and raised once as a single
``ImproperlyConfigured`` so a misconfigured container fails fast at startup
with an actionable message - never half-started, never with secrets leaked
into logs.

Validation is profile-aware:

* ``development`` / ``test``: type checks for any var that is present, throttle
  rate format, URL scheme sanity. Permissive (dev defaults are allowed).
* ``staging`` / ``production``: additionally require the presence of the
  critical secret/connection variables, forbid placeholder secrets and debug
  mode (production), and enforce cross-variable coherence (e.g. issuer/URL,
  allowed hosts, DB scheme).
"""

from __future__ import annotations

import os
import re

_RATE_RE = re.compile(r'^\d+/(second|minute|hour|day)$')

_PLACEHOLDER_SECRETS: tuple[str, ...] = (
    'unsafe-local-development-key-change-me',
    'replace_me',
    'replace-with-at-least-50-random-characters',
    'your-supabase-jwt-secret',
    'ci-secret-key-that-is-long-enough-for-testing-only',
)

#: Vars that must be set in staging and production.
_REQUIRED_STRICT = (
    'DJANGO_SECRET_KEY',
    'DJANGO_ALLOWED_HOSTS',
    'DATABASE_URL',
    'SUPABASE_URL',
    'SUPABASE_JWT_SECRET',
    'SUPABASE_JWT_ISSUER',
    'SUPABASE_JWT_AUDIENCE',
    'SUPABASE_WEBHOOK_SIGNING_SECRET',
    'STRIPE_SECRET_KEY',
    'STRIPE_WEBHOOK_SECRET',
)

#: Secret-shaped vars that must not be placeholder text outside development/test.
_SECRET_ENV = (
    'DJANGO_SECRET_KEY',
    'SUPABASE_JWT_SECRET',
    'SUPABASE_WEBHOOK_SIGNING_SECRET',
    'N8N_SENTRY_RELAY_SECRET',
    'STRIPE_SECRET_KEY',
    'STRIPE_WEBHOOK_SECRET',
    'CLOUDINARY_API_SECRET',
)

_INT_ENV = (
    'AGENT_MAX_ITERATIONS',
    'AI_GATEWAY_MAX_LATENCY_MS',
    'CLOUDINARY_MAX_UPLOAD_BYTES',
    'DATABASE_CONN_MAX_AGE',
    'VECTOR_EMBEDDING_DIMENSIONS',
    'RAG_CHUNK_SIZE',
    'RAG_CHUNK_OVERLAP',
    'RAG_TOP_K',
    'RAG_RERANK_TOP_K',
    'RAG_MAX_EXTRACTED_BYTES',
    'AUDIT_EVENT_RETENTION_DAYS',
    'SAFETY_EVENT_RETENTION_DAYS',
    'WEBHOOK_MAX_RETRIES',
    'WEBHOOK_RETRY_BASE_DELAY',
)

_FLOAT_ENV = (
    'AI_GATEWAY_MAX_COST_USD',
    'BILLING_CREDIT_VALUE_USD',
    'BILLING_FX_BUFFER',
    'BILLING_MARGIN_MULTIPLIER',
    'VECTOR_MIN_SIMILARITY',
    'RAG_SIMILARITY_THRESHOLD',
    'RAG_URL_FETCH_TIMEOUT_SECONDS',
    'SENTRY_TRACES_SAMPLE_RATE',
    'SENTRY_PROFILES_SAMPLE_RATE',
)

_FRACTION_ENV: dict[str, tuple[float, float]] = {
    'VECTOR_MIN_SIMILARITY': (0.0, 1.0),
    'RAG_SIMILARITY_THRESHOLD': (0.0, 1.0),
    'SENTRY_TRACES_SAMPLE_RATE': (0.0, 1.0),
    'SENTRY_PROFILES_SAMPLE_RATE': (0.0, 1.0),
}

_THROTTLE_ENV = (
    'THROTTLE_CHAT',
    'THROTTLE_IMAGES',
    'THROTTLE_EMBEDDINGS',
    'THROTTLE_CONVERSATIONS',
    'THROTTLE_RESEARCH',
    'THROTTLE_BURST',
)

_URL_ENV = 'CORS_ALLOWED_ORIGINS', 'CSRF_TRUSTED_ORIGINS'

_PROFILES = ('development', 'test', 'staging', 'production')


def _val(name: str, default: str | None = None) -> str:
    return os.getenv(name, default)


def _check_int(problems: list[str], name: str) -> None:
    value = _val(name)
    if value is None or value == '':
        return
    try:
        int(value)
    except ValueError:
        problems.append(f'{name} must be an integer, got {value!r}.')


def _check_float(problems: list[str], name: str) -> None:
    value = _val(name)
    if value is None or value == '':
        return
    try:
        float(value)
    except ValueError:
        problems.append(f'{name} must be a number, got {value!r}.')
        return
    bounds = _FRACTION_ENV.get(name)
    if bounds is not None:
        lo, hi = bounds
        number = float(value)
        if not (lo <= number <= hi):
            problems.append(f'{name} must be within [{lo}, {hi}], got {value!r}.')


def _check_origin(problems: list[str], name: str) -> None:
    value = _val(name)
    if not value:
        return
    for origin in (part.strip() for part in value.split(',') if part.strip()):
        if not origin.startswith(('http://', 'https://')):
            problems.append(f'{name} entry {origin!r} is not a valid origin URL.')


def _check_placeholder(problems: list[str], name: str) -> None:
    value = _val(name)
    if value and any(placeholder in value.lower() for placeholder in _PLACEHOLDER_SECRETS):
        problems.append(f'{name} still contains a placeholder value; refusing to start.')
    if name == 'DJANGO_SECRET_KEY' and value and len(value) < 32:
        problems.append('DJANGO_SECRET_KEY must be at least 32 characters long.')


def validate_environment(profile: str) -> list[str]:
    """Return the list of configuration problems for ``profile`` (empty = valid)."""
    if profile not in _PROFILES:
        raise ValueError(f'Unknown profile {profile!r}. Valid: {list(_PROFILES)}')

    problems: list[str] = []
    strict = profile in {'staging', 'production'}

    for name in _INT_ENV:
        _check_int(problems, name)
    for name in _FLOAT_ENV:
        _check_float(problems, name)
    for name in _THROTTLE_ENV:
        value = _val(name)
        if value and not _RATE_RE.match(value):
            problems.append(f'{name} does not match <count>/<period>, got {value!r}.')
    for name in _URL_ENV:
        _check_origin(problems, name)

    if not strict:
        return problems

    for name in _REQUIRED_STRICT:
        if not _val(name):
            problems.append(f'Missing required environment variable: {name}.')

    for name in _SECRET_ENV:
        _check_placeholder(problems, name)

    debug = _val('DJANGO_DEBUG', '0').lower() in {'1', 'true', 'yes', 'on'}
    if profile == 'production' and debug:
        problems.append('DJANGO_DEBUG must not be enabled in production.')

    hosts = _val('DJANGO_ALLOWED_HOSTS', '')
    if not hosts:
        problems.append('DJANGO_ALLOWED_HOSTS is required in production/staging.')
    else:
        if '*' in hosts.split(','):
            problems.append('DJANGO_ALLOWED_HOSTS may not contain "*" in production/staging.')

    database_url = _val('DATABASE_URL', '')
    if database_url and not database_url.startswith(('postgres://', 'postgresql://')):
        problems.append('DATABASE_URL must be a PostgreSQL connection string.')

    supabase_url = _val('SUPABASE_URL', '')
    issuer = _val('SUPABASE_JWT_ISSUER', '')
    if supabase_url and issuer and not issuer.startswith(supabase_url.rstrip('/')):
        problems.append('SUPABASE_JWT_ISSUER must start with SUPABASE_URL.')

    return problems


def validate_settings(profile: str) -> None:
    """Run :func:`validate_environment` and raise on any problem."""
    from django.core.exceptions import ImproperlyConfigured

    problems = validate_environment(profile)
    if problems:
        details = '\n'.join(f'  - {problem}' for problem in problems)
        raise ImproperlyConfigured(
            f'Invalid configuration for profile {profile!r}:\n{details}'
        )