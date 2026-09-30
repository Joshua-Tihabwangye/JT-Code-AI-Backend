from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"

    def ready(self) -> None:
        # Import drf-spectacular extensions during Django app initialization.
        from apps.core import schema  # noqa: F401
