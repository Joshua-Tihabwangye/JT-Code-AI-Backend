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

# Deterministic, offline embedding provider for the test suite. Real pgvector and
# full-text search still run against the Supabase test database.
RAG_EMBEDDING_PROVIDER = "echo"
RAG_EMBEDDING_MAX_RETRIES = 0
# Model reranking/judging go through the AI gateway; tests opt in with a mock.
RAG_RERANKER = "deterministic"
RAG_JUDGE = "deterministic"

# Unsubscribed tenants fall back to no plan in tests (the seeded "free" plan's
# quotas and concurrency limits would otherwise apply to every test tenant);
# plan behaviour is exercised explicitly.
BILLING_DEFAULT_PLAN = ""
STRIPE_SECRET_KEY = "sk_test_jtcode_unit_tests"  # nosec B105 # pragma: allowlist secret
STRIPE_WEBHOOK_SECRET = "whsec_jtcode_unit_tests"  # nosec B105 # pragma: allowlist secret
FRONTEND_URL = "https://app.example.test"

# Deterministic offline chat backend for the test suite (no SDK/key required).
AI_PROVIDER = "echo"

validate_settings("test")
