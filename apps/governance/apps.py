from django.apps import AppConfig


class GovernanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.governance"
    verbose_name = "Governance & Compliance"

    def ready(self) -> None:
        from django.contrib.admin.models import LogEntry
        from django.db.models.signals import post_save

        from apps.governance.audit import audit_admin_log_entry

        post_save.connect(audit_admin_log_entry, sender=LogEntry, dispatch_uid="jt-audit-admin-log")
