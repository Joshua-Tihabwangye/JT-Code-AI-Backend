from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

ANALYTICS_VIEWS = (
    "analytics_job_summary",
    "analytics_usage_ledger",
    "analytics_billing_summary",
    "analytics_conversation_summary",
    "analytics_asset_summary",
)

REQUIRED_RUNBOOK_TEXT = (
    "Point-in-Time Recovery",
    "sslmode=require",
    "RTO <= 4 hours",
    "RPO <= 1 hour",
)


class Command(BaseCommand):
    help = "Run local restore-drill verification for Phase 3 data foundation."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--format",
            choices=("json", "text"),
            default="text",
            help="Output format.",
        )
        parser.add_argument(
            "--prepare-test-db",
            action="store_true",
            help="Run migrations first for ephemeral local/test databases.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["prepare_test_db"]:
            call_command("migrate", verbosity=0, interactive=False)

        report = {
            "database_vendor": connection.vendor,
            "runbook": self._check_runbook(),
            "analytics_views": self._check_analytics_views(),
        }
        failures = [
            f"{section}.{name}: {detail}"
            for section, checks in report.items()
            if isinstance(checks, dict)
            for name, detail in checks.items()
            if detail is not True
        ]
        if failures:
            raise CommandError("Restore drill verification failed:\n" + "\n".join(failures))

        if options["format"] == "json":
            self.stdout.write(json.dumps(report, indent=2))
            return
        self.stdout.write(self.style.SUCCESS("Restore drill verification passed."))

    def _check_runbook(self) -> dict[str, bool | str]:
        path = Path(settings.BASE_DIR) / "docs" / "BACKUP_RESTORE_RUNBOOK.md"
        if not path.exists():
            return {"exists": "docs/BACKUP_RESTORE_RUNBOOK.md is missing."}
        text = path.read_text(encoding="utf-8")
        checks: dict[str, bool | str] = {"exists": True}
        for required in REQUIRED_RUNBOOK_TEXT:
            checks[required] = required in text or f"Missing required runbook text: {required}"
        for view_name in ANALYTICS_VIEWS:
            checks[view_name] = view_name in text or f"Runbook does not mention {view_name}."
        return checks

    def _check_analytics_views(self) -> dict[str, bool | str]:
        checks: dict[str, bool | str] = {}
        with connection.cursor() as cursor:
            for view_name in ANALYTICS_VIEWS:
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {view_name}")  # nosec B608 - constant view names.
                    cursor.fetchone()
                except Exception as exc:
                    checks[view_name] = str(exc)
                else:
                    checks[view_name] = True
        return checks
