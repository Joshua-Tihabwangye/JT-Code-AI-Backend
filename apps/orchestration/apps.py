from django.apps import AppConfig


class OrchestrationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.orchestration"
    verbose_name = "n8n Orchestration"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        from apps.orchestration.registry import register_after_migrate

        # Versioned definitions in n8n/workflows/ are registered on every migrate.
        post_migrate.connect(register_after_migrate, sender=self, dispatch_uid="jt-n8n-register")
