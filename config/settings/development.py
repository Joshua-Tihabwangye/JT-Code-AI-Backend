from config.settings.base import *  # noqa: F403,F405
from config.settings.validation import validate_settings

DEBUG = True
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

validate_settings('development')