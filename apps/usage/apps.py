from django.apps import AppConfig


class UsageConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.usage"
    verbose_name = "Usage metering"

    def ready(self) -> None:
        from apps.usage import signals  # noqa: F401 - connects ledger-purge signal
