from django.apps import AppConfig


class GovernanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.governance"
    verbose_name = "Governance & Compliance"

    def ready(self) -> None:
        from django.contrib.admin.models import LogEntry
        from django.db.models.signals import post_migrate, post_save

        from apps.governance.audit import audit_admin_log_entry
        from apps.governance.data_api import enable_row_level_security

        post_save.connect(audit_admin_log_entry, sender=LogEntry, dispatch_uid="jt-audit-admin-log")
        # Tables created after migration 0011 need RLS too; re-applied on every migrate.
        post_migrate.connect(enable_row_level_security, sender=self, dispatch_uid="jt-data-api-rls")
