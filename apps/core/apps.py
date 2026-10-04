from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"

    def ready(self) -> None:
        # Import drf-spectacular extensions during Django app initialization.
        from apps.core import schema  # noqa: F401
        from apps.core.metrics import connect_celery_signals, connect_model_signals
        from apps.core.tracing import configure_tracing

        connect_celery_signals()
        connect_model_signals()
        # Must run before the WSGI/ASGI handler builds the middleware chain.
        configure_tracing()
