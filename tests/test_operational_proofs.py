from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.management import call_command

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _strict_env() -> dict[str, str]:
    return {
        **os.environ,
        "DJANGO_SECRET_KEY": "ci-Key9aB3dE5fG7hI1jK2LmN4oP6qR8sT0uVxYz-0123456789",
        "DJANGO_ALLOWED_HOSTS": "api.example.com",
        "CORS_ALLOWED_ORIGINS": "https://app.example.com",
        "CSRF_TRUSTED_ORIGINS": "https://app.example.com",
        "DATABASE_URL": "postgresql://user:pass@db.example.com:5432/jtcode?sslmode=verify-full",
        "SUPABASE_URL": "https://project.supabase.co",
        "SUPABASE_JWKS_URL": "https://project.supabase.co/auth/v1/.well-known/jwks.json",
        "SUPABASE_SECRET_KEY": "sb_secret_ci9mY7Kq2Vx5Zp8Lr3",
        "SUPABASE_JWT_SECRET": "",
        "WEBHOOK_ALLOWED_HOSTS": "callbacks.example.com",
        "WEBHOOK_SIGNING_SECRET": "ci-outbound-callback-signing-secret",
        "SUPABASE_JWT_ISSUER": "https://project.supabase.co/auth/v1",
        "SUPABASE_JWT_AUDIENCE": "authenticated",
        "SUPABASE_WEBHOOK_SIGNING_SECRET": "supabase-webhook-secret",
        "REDIS_URL": "rediss://redis.example.com:6379/0",
        "CELERY_BROKER_URL": "rediss://redis.example.com:6379/1",
        "CELERY_RESULT_BACKEND": "rediss://redis.example.com:6379/2",
        "KAFKA_BOOTSTRAP_SERVERS": "kafka.example.com:9093",
        "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
        "KAFKA_SASL_MECHANISM": "SCRAM-SHA-512",
        "KAFKA_SASL_USERNAME": "ci",
        "KAFKA_SASL_PASSWORD": "ci-kafka-password",
        "IMAGEKIT_PUBLIC_KEY": "public_key",
        "IMAGEKIT_PRIVATE_KEY": "private_key",
        "IMAGEKIT_ENDPOINT_URL": "https://ik.imagekit.io/jt-code",
        "N8N_BASE_URL": "https://n8n.example.com",
        "N8N_API_KEY": "ci-n8n-api-key",
        "N8N_WEBHOOK_SECRET": "ci-n8n-webhook-secret",
        "N8N_SENTRY_RELAY_SECRET": "ci-n8n-sentry-relay-secret",
        "STRIPE_SECRET_KEY": "sk_live_ci9mY7Kq2Vx5Zp8Lr3",
        "STRIPE_WEBHOOK_SECRET": "whsec_ci9mY7Kq2Vx5Zp8Lr3",
        "SENTRY_DSN": "https://public@example.ingest.sentry.io/1",
        "SENTRY_ENVIRONMENT": "production",
        "AI_PROVIDER": "disabled",
        "TOOL_CREDENTIALS_ENCRYPTION_KEYS": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        "GEMINI_API_KEY": "gemini-production-key-9mY7Kq2V",
        "RAG_EMBEDDING_PROVIDER": "gemini",
        "FRONTEND_URL": "https://app.example.com",
        "DJANGO_DEBUG": "false",
    }


@pytest.mark.parametrize(
    "settings_module",
    (
        "config.settings.development",
        "config.settings.test",
        "config.settings.staging",
        "config.settings.production",
    ),
)
def test_django_settings_profile_starts_with_valid_environment(settings_module):
    result = subprocess.run(
        [sys.executable, "manage.py", "check", f"--settings={settings_module}"],
        cwd=PROJECT_ROOT,
        env=_strict_env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.django_db
def test_restore_drill_check_command_passes(capsys):
    call_command("restore_drill_check", format="json")

    output = capsys.readouterr().out
    assert "analytics_job_summary" in output
    assert "analytics_asset_summary" in output


def test_makefile_exposes_ci_local_and_restore_drill_targets():
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "ci-local:" in makefile
    assert "detect-secrets-hook --baseline .secrets.baseline" in makefile
    assert "bandit -q -r apps config manage.py" in makefile
    assert "pip-audit --strict" in makefile
    assert "restore-drill:" in makefile
    assert "restore_drill_check" in makefile
