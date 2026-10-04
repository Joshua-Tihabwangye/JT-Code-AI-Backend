"""Celery/Kafka saturation drill: measured drain throughput and latency per queue.

    python manage.py saturation_drill --tasks-per-queue 2000 --max-p95-ms 5000
    python manage.py saturation_drill --kafka-events 10000 --skip-celery

Run it against staging with the production worker topology; the report feeds
``manage.py capacity_plan``.
"""

from __future__ import annotations

import json
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.operations.drills import run_celery_saturation, run_kafka_saturation
from apps.operations.evidence import recorded
from apps.operations.models import VerificationRun


class Command(BaseCommand):
    help = "Flood Celery queues (and optionally Kafka) and measure throughput and latency."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--queues", default=",".join(q.name for q in settings.CELERY_TASK_QUEUES))
        parser.add_argument("--tasks-per-queue", type=int, default=500)
        parser.add_argument("--kafka-events", type=int, default=0)
        parser.add_argument("--skip-celery", action="store_true")
        parser.add_argument("--timeout", type=float, default=600.0)
        parser.add_argument("--max-p95-ms", type=float, default=10_000.0)
        parser.add_argument("--min-throughput", type=float, default=1.0, help="Tasks/second per queue.")

    def handle(self, *args: Any, **options: Any) -> None:
        queues = [q for q in options["queues"].split(",") if q]
        with recorded(VerificationRun.Kind.SATURATION_DRILL, parameters=dict(options)) as run:
            if not options["skip_celery"]:
                celery = run_celery_saturation(
                    queues=queues, tasks_per_queue=options["tasks_per_queue"], timeout=options["timeout"]
                )
                run.summary["celery"] = celery
                for queue, result in celery["queues"].items():
                    if result["completed"] < result["sent"]:
                        run.fail(f"{queue}: only {result['completed']}/{result['sent']} tasks completed")
                    if (result["latencyP95Ms"] or 0) > options["max_p95_ms"]:
                        run.fail(f"{queue}: p95 latency {result['latencyP95Ms']} ms over budget")
                    if result["throughputPerSecond"] < options["min_throughput"]:
                        run.fail(f"{queue}: throughput {result['throughputPerSecond']}/s under budget")
            if options["kafka_events"]:
                kafka = run_kafka_saturation(events=options["kafka_events"], timeout=options["timeout"])
                run.summary["kafka"] = kafka
                if kafka["produceFailures"] or kafka["consumed"] < kafka["produced"]:
                    run.fail("Kafka drill lost or failed to deliver events")
        self.stdout.write(json.dumps({**run.summary, "failures": run.failures}, indent=2, default=str))
        if run.failures:
            raise CommandError("Saturation drill failed: " + "; ".join(run.failures))
