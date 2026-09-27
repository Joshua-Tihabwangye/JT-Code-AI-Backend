from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

ANALYTICS_ROLE = "jt_code_analytics_reader"
ANALYTICS_VIEWS = (
    "analytics_job_summary",
    "analytics_usage_ledger",
    "analytics_billing_summary",
    "analytics_conversation_summary",
    "analytics_asset_summary",
)
CANONICAL_TABLES = (
    "identity_user",
    "identity_organization",
    "conversations_conversation",
    "jobs_job",
    "assets_asset",
    "billing_creditledger",
    "governance_auditevent",
)
REQUIRED_RUNBOOK_TEXT = (
    "Point-in-Time Recovery",
    "sslmode=require",
    "RTO <= 4 hours",
    "RPO <= 1 hour",
    "jt_code_analytics_reader",
    "--restore-database-url",
)


class Command(BaseCommand):
    help = "Verify analytics access and optionally execute an isolated PostgreSQL restore drill."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--format", choices=("json", "text"), default="text")
        parser.add_argument("--prepare-test-db", action="store_true", help="Run migrations first.")
        parser.add_argument("--restore-database-url", help="Disposable PostgreSQL database to overwrite.")
        parser.add_argument(
            "--require-source-data",
            action="store_true",
            help="Fail unless every canonical table contains at least one source row.",
        )
        parser.add_argument(
            "--confirm-restore-target",
            action="store_true",
            help="Required acknowledgement before pg_restore overwrites the target database.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["prepare_test_db"]:
            call_command("migrate", verbosity=0, interactive=False)
        report: dict[str, Any] = {
            "database_vendor": connection.vendor,
            "runbook": self._check_runbook(),
            "analytics_views": self._check_analytics_views(),
            "analytics_access": self._check_analytics_access(),
        }
        target = options["restore_database_url"]
        if target:
            report["restore"] = self._restore_and_compare(
                target, options["confirm_restore_target"], options["require_source_data"]
            )
        failures = [
            f"{section}.{name}: {detail}"
            for section, checks in report.items()
            if isinstance(checks, dict)
            for name, detail in checks.items()
            if detail is not True
        ]
        if failures:
            raise CommandError("Restore drill verification failed:\n" + "\n".join(failures))
        output = (
            json.dumps(report, indent=2)
            if options["format"] == "json"
            else "Restore drill verification passed."
        )
        self.stdout.write(self.style.SUCCESS(output))

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
                    cursor.execute(f"SELECT COUNT(*) FROM {view_name}")  # nosec B608 -- constants.
                    cursor.fetchone()
                except Exception as exc:
                    checks[view_name] = str(exc)
                else:
                    checks[view_name] = True
        return checks

    def _check_analytics_access(self) -> dict[str, bool | str]:
        if connection.vendor != "postgresql":
            return {"postgresql_required": True}
        checks: dict[str, bool | str] = {}
        with connection.cursor() as cursor:
            for view_name in ANALYTICS_VIEWS:
                cursor.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", [ANALYTICS_ROLE, view_name])
                checks[f"select_{view_name}"] = bool(cursor.fetchone()[0])
            for table_name in CANONICAL_TABLES:
                cursor.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", [ANALYTICS_ROLE, table_name])
                checks[f"no_table_select_{table_name}"] = not bool(cursor.fetchone()[0])
        return checks

    def _restore_and_compare(
        self, target_url: str, confirmed: bool, require_source_data: bool
    ) -> dict[str, bool | str]:
        if connection.vendor != "postgresql":
            return {"postgresql_required": "Restore execution requires PostgreSQL."}
        if not confirmed:
            return {"confirmation": "Pass --confirm-restore-target to overwrite the isolated target."}
        source_url = os.environ.get("DATABASE_URL", "")
        if not source_url:
            return {"source_url": "DATABASE_URL is required for pg_dump."}
        if self._same_database(source_url, target_url):
            return {"target": "Restore target must be a different database from DATABASE_URL."}
        if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
            return {"postgres_client": "pg_dump and pg_restore must be installed."}
        with tempfile.TemporaryDirectory(prefix="jt-code-restore-") as tempdir:
            dump_file = str(Path(tempdir) / "source.dump")
            self._ensure_target_analytics_role(target_url)
            self._run_client(["pg_dump", "--format=custom", "--no-owner", "--file", dump_file, source_url])
            self._run_client(
                ["pg_restore", "--clean", "--if-exists", "--no-owner", "--dbname", target_url, dump_file]
            )
            source_counts = self._row_counts(source_url)
            target_counts = self._row_counts(target_url)
            target_access = self._target_analytics_access(target_url)
        checks: dict[str, bool | str] = {"executed": True}
        if require_source_data:
            for table_name, count in source_counts.items():
                checks[f"source_has_rows_{table_name}"] = count > 0
        for table_name, count in source_counts.items():
            checks[f"row_count_{table_name}"] = target_counts.get(table_name) == count
        checks.update(target_access)
        return checks

    @staticmethod
    def _ensure_target_analytics_role(database_url: str) -> None:
        import psycopg

        with psycopg.connect(database_url, autocommit=True) as database, database.cursor() as cursor:
            cursor.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jt_code_analytics_reader') THEN
                        CREATE ROLE jt_code_analytics_reader
                            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
                    END IF;
                END
                $$;
                """
            )

    @staticmethod
    def _target_analytics_access(database_url: str) -> dict[str, bool]:
        import psycopg

        checks: dict[str, bool] = {}
        with psycopg.connect(database_url) as database, database.cursor() as cursor:
            for view_name in ANALYTICS_VIEWS:
                cursor.execute(f"SELECT COUNT(*) FROM {view_name}")  # nosec B608 -- constants.
                cursor.fetchone()
                cursor.execute("SELECT has_table_privilege(%s, %s, 'SELECT')", [ANALYTICS_ROLE, view_name])
                checks[f"target_select_{view_name}"] = bool(cursor.fetchone()[0])
        return checks

    @staticmethod
    def _same_database(source_url: str, target_url: str) -> bool:
        source, target = urlparse(source_url), urlparse(target_url)
        source_identity = (source.hostname, source.port or 5432, source.path)
        target_identity = (target.hostname, target.port or 5432, target.path)
        return source_identity == target_identity

    @staticmethod
    def _run_client(command: list[str]) -> None:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=900)
        if result.returncode:
            raise CommandError("PostgreSQL backup/restore client failed; inspect its secured CI logs.")

    @staticmethod
    def _row_counts(database_url: str) -> dict[str, int]:
        import psycopg

        with psycopg.connect(database_url) as database, database.cursor() as cursor:
            counts = {}
            for table_name in CANONICAL_TABLES:
                cursor.execute(f"SELECT COUNT(*) FROM {table_name}")  # nosec B608 -- constants.
                counts[table_name] = int(cursor.fetchone()[0])
            return counts
