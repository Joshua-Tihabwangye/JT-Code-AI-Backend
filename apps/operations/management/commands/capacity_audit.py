"""Capacity audit: connection budget at full autoscale plus a live database health check.

    python manage.py capacity_audit --pooler-max-clients 1000 --format=json

Fails when the worst-case client connections exceed the pooler's client limit,
or when a foreign key on a large table has no index.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.operations.capacity import connection_budget, database_audit, dumps
from apps.operations.evidence import recorded
from apps.operations.models import VerificationRun


class Command(BaseCommand):
    help = "Audit connection budget, indexes, hotspots and partition candidates; record the evidence."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--environment", default="production", help="Overlay used for autoscaling ceilings."
        )
        parser.add_argument(
            "--pooler-max-clients",
            type=int,
            default=0,
            help="Supavisor max client connections for the project's compute size (0 = skip the check).",
        )
        parser.add_argument("--min-fk-rows", type=int, default=10_000)
        parser.add_argument("--format", choices=("json", "text"), default="text")

    def handle(self, *args: Any, **options: Any) -> None:
        with recorded(VerificationRun.Kind.CAPACITY_AUDIT, parameters=dict(options)) as run:
            budget = connection_budget(options["environment"])
            audit = database_audit(min_fk_rows=options["min_fk_rows"])
            run.summary = {"connectionBudget": budget, "database": audit}
            limit = options["pooler_max_clients"]
            if limit and budget["totalMaxClientConnections"] > limit:
                run.fail(
                    f"worst-case {budget['totalMaxClientConnections']} client connections exceed the pooler "
                    f"limit {limit}; lower autoscaling ceilings/concurrency or raise the compute size"
                )
            for row in audit["unindexedForeignKeysOnLargeTables"]:
                run.fail(f"unindexed foreign key {row['table']}.{row['column']} ({row['rows']} rows)")
        if options["format"] == "json":
            self.stdout.write(dumps({**run.summary, "failures": run.failures}))
        else:
            self.stdout.write(
                f"worst-case client connections: {budget['totalMaxClientConnections']} "
                f"(database max_connections {audit['maxConnections']}, in use {audit['connectionsInUse']})"
            )
            for key in (
                "unindexedForeignKeys",
                "unusedIndexes",
                "sequentialScanHotspots",
                "partitionCandidates",
            ):
                self.stdout.write(f"{key}: {len(audit[key])}")
        if run.failures:
            raise CommandError("Capacity audit failed: " + "; ".join(run.failures))
