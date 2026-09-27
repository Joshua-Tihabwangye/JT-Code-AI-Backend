from config.settings.base import *  # noqa: F403,F405
from config.settings.validation import validate_settings

SECRET_KEY = env("DJANGO_SECRET_KEY", "unsafe-local-development-key-change-me")  # noqa: F405
DEBUG = env_bool("DJANGO_DEBUG", True)  # noqa: F405
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")  # noqa: F405
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

validate_settings('development')