from __future__ import annotations

from pathlib import Path

from django.conf import settings

from config.settings.validation import validate_environment

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _strict_env() -> dict[str, str]:
    return {
        "DJANGO_SECRET_KEY": "x" * 50,
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


def test_production_validation_rejects_insecure_database_and_wildcard_hosts(monkeypatch):
    for key, value in _strict_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db.example.com:5432/jtcode")
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "api.example.com,*")

    problems = validate_environment("production")

    assert "DATABASE_URL must set sslmode to require, verify-ca, or verify-full in deployable environments." in problems
    assert 'DJANGO_ALLOWED_HOSTS may not contain "*" in production/staging.' in problems


def test_ci_security_scans_are_gating():
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "detect-secrets scan --all-files" in ci
    assert "bandit -q -r apps config manage.py" in ci
    assert "pip-audit --strict" in ci
    assert "|| true" not in ci
