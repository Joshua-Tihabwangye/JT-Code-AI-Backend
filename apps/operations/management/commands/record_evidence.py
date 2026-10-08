"""Store a report produced outside the cluster (load test, chaos experiment) as evidence.

kubectl exec -i deploy/jt-code-api -- python manage.py record_evidence load_test - < report.json
"""

from __future__ import annotations

import json
import sys
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.operations.evidence import environment_name
from apps.operations.models import VerificationRun


class Command(BaseCommand):
    help = "Record a JSON report ({'passed': bool, ...}) as a VerificationRun."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("kind", choices=[choice for choice, _ in VerificationRun.Kind.choices])
        parser.add_argument("report", help="Path to the JSON report, or - for stdin.")

    def handle(self, *args: Any, **options: Any) -> None:
        raw = sys.stdin.read() if options["report"] == "-" else open(options["report"]).read()  # noqa: SIM115
        try:
            report = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON report: {exc}") from exc
        if not isinstance(report, dict) or "passed" not in report:
            raise CommandError("The report must be an object with a boolean 'passed'.")
        run = VerificationRun.objects.create(
            kind=options["kind"],
            status=VerificationRun.Status.PASSED
            if report["passed"] is True
            else VerificationRun.Status.FAILED,
            environment=environment_name(),
            git_sha=str(report.get("gitSha", "")),
            parameters=report.get("parameters") or {},
            summary=report,
            failures=list(report.get("failures") or []),
            finished_at=timezone.now(),
        )
        self.stdout.write(f"recorded {run.kind} ({run.status}) {run.id}")
