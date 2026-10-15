import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
# Read by config.settings.base: under ASGI, sync views run on worker threads and a
# persistent connection per thread is never reaped, so this process must not keep them.
os.environ["JT_CODE_SERVER_INTERFACE"] = "asgi"
application = get_asgi_application()
