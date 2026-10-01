from django.apps import AppConfig


class ToolsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tools"

    def ready(self) -> None:
        # Register integration tool specs once the app registry is ready.
        from apps.tools.adapters import register_all

        register_all()
