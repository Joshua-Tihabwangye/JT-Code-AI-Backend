from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings

from config.settings.validation import validate_environment

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _strict_env() -> dict[str, str]:
    return {
        "DJANGO_SECRET_KEY": "aB3dE5fG7hI9jK1LmN2oP4qR6sT8uV0wXyZ-0123456789abcde",
        "DJANGO_ALLOWED_HOSTS": "api.example.com",
        "CORS_ALLOWED_ORIGINS": "https://app.example.com",
        "CSRF_TRUSTED_ORIGINS": "https://app.example.com",
        "DATABASE_URL": "postgresql://user:pass@db.example.com:5432/jtcode?sslmode=require",
        "SUPABASE_URL": "https://project.supabase.co",
        "SUPABASE_JWT_SECRET": "supabase-secret-value",
        "SUPABASE_JWT_ISSUER": "https://project.supabase.co/auth/v1",
        "SUPABASE_JWT_AUDIENCE": "authenticated",
        "SUPABASE_WEBHOOK_SIGNING_SECRET": "supabase-webhook-secret",
        "REDIS_URL": "rediss://redis.example.com:6380/0",
        "CELERY_BROKER_URL": "rediss://redis.example.com:6380/1",
        "CELERY_RESULT_BACKEND": "rediss://redis.example.com:6380/2",
        "KAFKA_BOOTSTRAP_SERVERS": "kafka.example.com:9093",
        "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
        "KAFKA_SASL_MECHANISM": "SCRAM-SHA-512",
        "KAFKA_SASL_USERNAME": "jt-code",
        "KAFKA_SASL_PASSWORD": "kafka-password",
        "IMAGEKIT_PUBLIC_KEY": "public_key",
        "IMAGEKIT_PRIVATE_KEY": "private_key",
        "IMAGEKIT_ENDPOINT_URL": "https://ik.imagekit.io/jt-code",
        "STRIPE_SECRET_KEY": "sk_live_9mY7Kq2Vx5Zp8Lr3",
        "STRIPE_WEBHOOK_SECRET": "whsec_9mY7Kq2Vx5Zp8Lr3",
        "N8N_BASE_URL": "https://n8n.example.com",
        "N8N_API_KEY": "n8n-api-key",
        "N8N_WEBHOOK_SECRET": "n8n-webhook-secret",
        "N8N_SENTRY_RELAY_SECRET": "n8n-sentry-relay-secret",
        "SENTRY_DSN": "https://public@example.ingest.sentry.io/1",
        "SENTRY_ENVIRONMENT": "production",
        "DJANGO_DEBUG": "false",
    }


def test_django_logging_configuration_is_loaded():
    assert settings.LOGGING["filters"]["request_context"]["()"] == "apps.core.logging.RequestContextFilter"
    assert settings.LOGGING["formatters"]["json"]["()"] == "apps.core.logging.JSONFormatter"


def test_production_validation_accepts_complete_strict_env(monkeypatch):
    for key, value in _strict_env().items():
        monkeypatch.setenv(key, value)

    assert validate_environment("production") == []


def test_production_validation_accepts_stronger_postgres_tls_modes(monkeypatch):
    for key, value in _strict_env().items():
        monkeypatch.setenv(key, value)
    for sslmode in ("verify-ca", "verify-full"):
        monkeypatch.setenv(
            "DATABASE_URL", f"postgresql://user:pass@db.example.com:5432/jtcode?sslmode={sslmode}"
        )
        assert validate_environment("production") == []


def test_production_validation_rejects_insecure_transport_and_wildcard_hosts(monkeypatch):
    for key, value in _strict_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db.example.com:5432/jtcode")
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "api.example.com, *")
    monkeypatch.setenv("REDIS_URL", "redis://redis.example.com:6379/0")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://app.example.com")
    monkeypatch.setenv("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT")
    monkeypatch.delenv("N8N_WEBHOOK_SECRET")

    problems = validate_environment("production")

    assert (
        "DATABASE_URL must set sslmode to require, verify-ca, or verify-full in deployable environments."
        in problems
    )
    assert 'DJANGO_ALLOWED_HOSTS may not contain "*" in production/staging.' in problems
    assert "REDIS_URL must use a TLS redis URL (rediss://) in deployable environments." in problems
    assert (
        "CORS_ALLOWED_ORIGINS entry 'http://app.example.com' must use HTTPS in deployable environments."
        in problems
    )
    assert "KAFKA_SECURITY_PROTOCOL must be SASL_SSL in deployable environments." in problems
    assert "Missing required environment variable: N8N_WEBHOOK_SECRET." in problems


def test_env_example_contains_only_live_variables_and_no_development_secrets():
    source = (PROJECT_ROOT / "config" / "settings" / "base.py").read_text(encoding="utf-8")
    source += (PROJECT_ROOT / "config" / "settings" / "development.py").read_text(encoding="utf-8")
    live = set(re.findall(r'env(?:_bool|_float|_list)?\("([A-Z0-9_]+)"', source))
    declared = {
        line.split("=", 1)[0]
        for line in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    assert declared == live
    assert (
        not {
            "FEATURE_FLAG_ENABLE_RAG",
            "STREAMLIT_SERVER_PORT",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "EMAIL_HOST_PASSWORD",
        }
        & declared
    )
    assert "postgres:postgres" not in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")


def test_ci_security_scans_are_gating():
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "detect-secrets-hook --baseline .secrets.baseline" in ci
    assert "python manage.py migrate --noinput --settings=config.settings.ci" in ci
    assert "python manage.py check --deploy --settings=config.settings.production" in ci
    assert "- run: mypy" in ci
    assert "python-version: '3.12.14'" in ci
    assert "bandit -q -r apps config manage.py" in ci
    assert "pip-audit --strict" in ci
    assert "|| true" not in ci


def test_json_logging_preserves_allowlisted_operational_fields():
    import json
    import logging

    from apps.core.logging import JSONFormatter

    record = logging.LogRecord("test", logging.INFO, __file__, 1, "completed", (), None)
    record.method = "GET"
    record.path = "/api/v1/health/live/"
    record.status_code = 200
    record.duration_ms = 1.25
    record.untrusted_body = "must not be serialized"
    payload = json.loads(JSONFormatter().format(record))

    assert payload["method"] == "GET"
    assert payload["path"] == "/api/v1/health/live/"
    assert payload["status_code"] == 200
    assert payload["duration_ms"] == 1.25
    assert "untrusted_body" not in payload
