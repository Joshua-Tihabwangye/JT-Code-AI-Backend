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
        "REDIS_URL": "redis://redis.example.com:6379/0",
        "CELERY_BROKER_URL": "redis://redis.example.com:6379/1",
        "CELERY_RESULT_BACKEND": "redis://redis.example.com:6379/2",
        "KAFKA_BOOTSTRAP_SERVERS": "kafka.example.com:9092",
        "IMAGEKIT_PUBLIC_KEY": "public_key",
        "IMAGEKIT_PRIVATE_KEY": "private_key",
        "IMAGEKIT_ENDPOINT_URL": "https://ik.imagekit.io/jt-code",
        "STRIPE_SECRET_KEY": "sk_live_placeholder_for_validation",
        "STRIPE_WEBHOOK_SECRET": "whsec_placeholder_for_validation",
        "DJANGO_DEBUG": "false",
    }


def test_django_logging_configuration_is_loaded():
    assert settings.LOGGING["filters"]["request_context"]["()"] == "apps.core.logging.RequestContextFilter"
    assert "request_id=%(request_id)s" in settings.LOGGING["formatters"]["jsonish"]["format"]


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

    assert "DATABASE_URL must include sslmode=require in production." in problems
    assert 'DJANGO_ALLOWED_HOSTS may not contain "*" in production/staging.' in problems


def test_ci_security_scans_are_gating():
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "detect-secrets scan --all-files" in ci
    assert "bandit -q -r apps config manage.py" in ci
    assert "pip-audit --strict" in ci
    assert "|| true" not in ci
