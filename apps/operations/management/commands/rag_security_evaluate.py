"""RAG security evaluation: tenant isolation, ACLs, deletion and prompt-injection containment."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.operations.evidence import recorded
from apps.operations.models import VerificationRun
from apps.operations.rag_security import run_security_evaluation


class Command(BaseCommand):
    help = "Evaluate RAG isolation and injection defences in throwaway tenants and record the evidence."

    def handle(self, *args: Any, **options: Any) -> None:
        with recorded(VerificationRun.Kind.RAG_SECURITY) as run:
            report = run_security_evaluation()
            run.summary = report.as_dict()
            for check, ok in report.checks.items():
                if not ok:
                    run.fail(check)
        self.stdout.write(json.dumps(run.summary, indent=2))
        if run.failures:
            raise CommandError("RAG security evaluation failed: " + ", ".join(run.failures))
