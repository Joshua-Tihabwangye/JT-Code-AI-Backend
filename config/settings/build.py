"""Image build profile: only ``collectstatic`` runs with it (no secrets, no database)."""

from config.settings.base import *  # noqa: F403

SECRET_KEY = "image-build-only-not-used-at-runtime"  # nosec B105  # pragma: allowlist secret
DEBUG = False
DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", "NAME": "unused"}}
