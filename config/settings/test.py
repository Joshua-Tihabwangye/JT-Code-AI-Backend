from config.settings.base import *  # noqa: F403
from config.settings.validation import validate_settings

SECRET_KEY = "test-only-secret-key-that-is-long-enough-for-django"  # nosec B105
DEBUG = False
HEALTHCHECK_EXTERNAL_DEPENDENCIES = False
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

# Provide default Supabase settings so tests can authenticate deterministically
# (never inherit live project values from the environment during tests)
SUPABASE_JWT_SECRET = "test-jwt-secret"  # nosec B105 - deterministic test-only signing secret.
SUPABASE_JWT_AUDIENCE = "authenticated"
SUPABASE_JWT_ISSUER = ""
SUPABASE_URL = ""
SUPABASE_WEBHOOK_SIGNING_SECRET = env("SUPABASE_WEBHOOK_SIGNING_SECRET", "")  # noqa: F405

# Deterministic, offline embedding provider for the test suite. The pgvector
# store itself is Postgres-only and unavailable on the SQLite test database.
RAG_EMBEDDING_PROVIDER = "echo"

# Deterministic offline chat backend for the test suite (no SDK/key required).
AI_PROVIDER = "echo"

validate_settings("test")
