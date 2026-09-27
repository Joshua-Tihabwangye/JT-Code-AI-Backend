from config.settings.base import *  # noqa: F403,F405
from config.settings.secure import *  # noqa: F403
from config.settings.validation import validate_settings

DEBUG = False
HEALTHCHECK_EXTERNAL_DEPENDENCIES = True
# Docker's in-container probe is not exposed through the public ingress.
ALLOWED_HOSTS = list(dict.fromkeys([*ALLOWED_HOSTS, "localhost", "127.0.0.1"]))

validate_settings("production")
