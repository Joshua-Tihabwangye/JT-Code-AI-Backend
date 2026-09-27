from config.settings.base import *  # noqa: F403,F405
from config.settings.secure import *  # noqa: F403
from config.settings.validation import validate_settings

# Staging exercises production behavior; debug pages must never be internet-facing.
DEBUG = False
HEALTHCHECK_EXTERNAL_DEPENDENCIES = True

validate_settings("staging")
