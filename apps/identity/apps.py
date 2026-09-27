from django.apps import AppConfig


class IdentityConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.identity"

    def ready(self):
        # Register lifecycle handlers after Django's app registry is ready.
        import apps.identity.signals  # noqa: F401
