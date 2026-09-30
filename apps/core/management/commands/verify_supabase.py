"""Read-only production evidence checks for a migrated Supabase database."""

from __future__ import annotations

import json
from typing import Any

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder

TENANT_MODELS = (
    "assets.Asset",
    "conversations.Conversation",
    "conversations.Message",
    "conversations.ChatRequest",
    "conversions.ConversionJob",
    "documents.Document",
    "jobs.Job",
)
VECTOR_TABLE = "knowledge_chunk"
VECTOR_COLUMN = "embedding"


class Command(BaseCommand):
    help = (
        "Read-only verification of PostgreSQL, pgvector, migration leaves, and tenant ownership. "
        "Run only after migrate against an isolated CI, staging, or restore database."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--format", choices=("json", "text"), default="text")
        parser.add_argument(
            "--allow-empty-vector-column",
            action="store_true",
            help=(
                "Do not require a pgvector embedding column (use only before the knowledge app is installed)."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ARG002
        report = {
            "postgresql": self._check_postgresql(),
            "migrations": self._check_migration_leaves(),
            "pgvector": self._check_pgvector(require_column=not options["allow_empty_vector_column"]),
            "tenant_ownership": self._check_tenant_ownership(),
            "data_api_lockdown": self._check_data_api_lockdown(),
        }
        failures = self._failures(report)
        if failures:
            message = "Supabase verification failed:\n" + "\n".join(f"- {item}" for item in failures)
            raise CommandError(message)
        if options["format"] == "json":
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        else:
            self.stdout.write(self.style.SUCCESS("Supabase verification passed."))

    @staticmethod
    def _check_postgresql() -> dict[str, bool]:
        return {"vendor_is_postgresql": connection.vendor == "postgresql"}

    @staticmethod
    def _check_migration_leaves() -> dict[str, bool]:
        loader = MigrationLoader(connection, ignore_no_migrations=True)
        applied = set(MigrationRecorder.Migration.objects.using(connection.alias).values_list("app", "name"))
        leaves = set(loader.graph.leaf_nodes())
        missing = sorted(leaves.difference(applied))
        checks: dict[str, bool] = {"all_leaf_migrations_applied": not missing}
        checks.update({f"applied:{app}.{name}": False for app, name in missing})
        return checks

    @staticmethod
    def _check_pgvector(*, require_column: bool) -> dict[str, bool]:
        if connection.vendor != "postgresql":
            return {"vector_extension": False, "embedding_column": not require_column}
        with connection.cursor() as cursor:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
            extension_enabled = bool(cursor.fetchone()[0])
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = %s AND column_name = %s AND udt_name = 'vector'
                )
                """,
                [VECTOR_TABLE, VECTOR_COLUMN],
            )
            embedding_column = bool(cursor.fetchone()[0])
        return {
            "vector_extension": extension_enabled,
            "embedding_column": embedding_column or not require_column,
        }

    @staticmethod
    def _check_tenant_ownership() -> dict[str, bool]:
        checks: dict[str, bool] = {}
        for label in TENANT_MODELS:
            model = apps.get_model(label)
            null_count = model._default_manager.filter(organization_id__isnull=True).count()
            checks[f"no_null_organization:{label}"] = null_count == 0
        return checks

    @staticmethod
    def _check_data_api_lockdown() -> dict[str, bool]:
        """Supabase anon/authenticated roles must not reach Django tables via PostgREST."""
        if connection.vendor != "postgresql":
            return {}
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_roles WHERE rolname IN ('anon', 'authenticated')")
            if not cursor.fetchone()[0]:
                return {"not_supabase_no_data_api_roles": True}
            cursor.execute(
                """
                SELECT count(DISTINCT table_name) FROM information_schema.role_table_grants
                WHERE table_schema = 'public' AND grantee IN ('anon', 'authenticated')
                """
            )
            granted = cursor.fetchone()[0]
            cursor.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND NOT rowsecurity")
            without_rls = cursor.fetchone()[0]
        return {
            "no_public_grants_to_data_api_roles": granted == 0,
            "row_level_security_on_all_public_tables": without_rls == 0,
        }

    @staticmethod
    def _failures(report: dict[str, dict[str, bool]]) -> list[str]:
        return [
            f"{section}.{name}"
            for section, checks in report.items()
            for name, succeeded in checks.items()
            if not succeeded
        ]
