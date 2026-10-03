from config.settings.base import *  # noqa: F403
from config.settings.validation import validate_settings

SECRET_KEY = "test-only-secret-key-that-is-long-enough-for-django"  # nosec B105
DEBUG = False
HEALTHCHECK_EXTERNAL_DEPENDENCIES = False
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
ASSET_LOCAL_FALLBACK_ENABLED = True
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "KEY_PREFIX": "jt-code:cache"},
    "rate_limits": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "KEY_PREFIX": "jt-code:rate-limit",
    },
    "job_locks": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "KEY_PREFIX": "jt-code:job-lock",
    },
}
DATABASES = runtime_connection_settings(  # noqa: F405
    database_url=env("TEST_DATABASE_URL", required=True)  # noqa: F405
)[0]

# Provide default Supabase settings so tests can authenticate deterministically
# (never inherit live project values from the environment during tests)
SUPABASE_JWT_SECRET = "test-jwt-secret"  # nosec B105 - deterministic test-only signing secret.
SUPABASE_JWT_AUDIENCE = "authenticated"
SUPABASE_JWT_ISSUER = ""
SUPABASE_URL = ""
SUPABASE_WEBHOOK_SIGNING_SECRET = env("SUPABASE_WEBHOOK_SIGNING_SECRET", "")  # noqa: F405
WEBHOOK_ALLOWED_HOSTS = ["callbacks.example.test"]
SUPABASE_SECRET_KEY = "sb_secret_test-only-admin-key"  # nosec B105
KAFKA_CONSUMER_RETRY_MAX_SECONDS = 0
WEBHOOK_SIGNING_SECRET = "test-outbound-callback-signing-secret"  # nosec B105

# Deterministic, offline embedding provider for the test suite.
RAG_EMBEDDING_PROVIDER = "echo"

# Deterministic offline chat backend for the test suite (no SDK/key required).
AI_PROVIDER = "echo"

validate_settings("test")
