"""Dead-letter recovery drill: poison -> DLQ -> fix -> replay -> consumed exactly once."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.operations.drills import run_dlq_drill
from apps.operations.evidence import recorded
from apps.operations.models import VerificationRun

_REQUIRED = (
    "deadLetterNotified",
    "replayedConsumed",
    "duplicateIgnored",
    "doubleReplayBlocked",
    "consumedOnce",
    "markedReplayed",
)


class Command(BaseCommand):
    help = "Run the DLQ recovery drill against this environment's database and record the evidence."

    def handle(self, *args: Any, **options: Any) -> None:
        with recorded(VerificationRun.Kind.DLQ_DRILL) as run:
            run.summary = run_dlq_drill()
            for check in _REQUIRED:
                if not run.summary.get(check):
                    run.fail(f"{check} is false")
        self.stdout.write(json.dumps({**run.summary, "failures": run.failures}, indent=2))
        if run.failures:
            raise CommandError("DLQ drill failed.")
