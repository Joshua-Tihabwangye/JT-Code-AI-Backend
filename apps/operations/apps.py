from django.apps import AppConfig


class OperationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.operations"
    verbose_name = "Production verification"

    def ready(self) -> None:
        from apps.operations import drills  # noqa: F401  (registers the DLQ drill handler)
