from django.apps import AppConfig


class VisualizeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.visualize"
    verbose_name = "Data visualization"

    def ready(self) -> None:
        # Registers the signal that drops a dataset's Postgres query table with it.
        from apps.visualize import store  # noqa: F401
