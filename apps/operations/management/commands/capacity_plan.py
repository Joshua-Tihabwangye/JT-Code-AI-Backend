"""Capacity plan for a target user population from *measured* throughput.

    python manage.py capacity_plan --users 100000 --format markdown

Per-unit throughput (requests/s per API pod, jobs/s per worker pod, events/s
per Kafka partition) comes from the latest passing load test and saturation
drill evidence, or explicit ``--measured-*`` overrides. The plan is compared
with the production overlay's autoscaling ceilings.
"""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.operations.capacity import (
    connection_budget,
    dumps,
    measured_inputs_from_evidence,
    plan,
    render_markdown,
)

DEFAULT_JOBS_PER_USER_HOUR = {
    "jobs.analysis": 6.0,  # chat/agent/RAG generations
    "jobs.ingestion": 0.5,
    "jobs.visualization": 0.3,
    "analytics.analysis": 0.2,
    "jobs.default": 2.0,
}


class Command(BaseCommand):
    help = "Size API pods, worker pools, DB connections and Kafka partitions for a user target."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--users", type=int, default=100_000)
        parser.add_argument("--daily-active-ratio", type=float, default=0.2)
        parser.add_argument("--peak-concurrency-ratio", type=float, default=0.1)
        parser.add_argument("--requests-per-minute", type=float, default=12.0, help="Per active user.")
        parser.add_argument("--events-per-request", type=float, default=0.5)
        parser.add_argument("--headroom", type=float, default=1.5)
        parser.add_argument("--measured-rps-per-api-pod", type=float, default=None)
        parser.add_argument("--measured-events-per-partition", type=float, default=None)
        parser.add_argument(
            "--measured-jobs-per-worker", default=None, help='JSON: {"jobs.analysis": 4.0, ...}'
        )
        parser.add_argument("--format", choices=("json", "markdown"), default="json")

    def handle(self, *args: Any, **options: Any) -> None:
        measured = measured_inputs_from_evidence()
        rps = options["measured_rps_per_api_pod"] or measured.get("measured_rps_per_api_pod")
        events = options["measured_events_per_partition"] or measured.get(
            "measured_events_per_partition_second"
        )
        jobs = (
            json.loads(options["measured_jobs_per_worker"])
            if options["measured_jobs_per_worker"]
            else measured.get("measured_jobs_per_worker_second")
        )
        if not rps or not jobs:
            raise CommandError(
                "No measured throughput: run loadtests/mix-100k.json and saturation_drill first "
                "(or pass --measured-* overrides)."
            )
        result = plan(
            users=options["users"],
            daily_active_ratio=options["daily_active_ratio"],
            peak_concurrency_ratio=options["peak_concurrency_ratio"],
            requests_per_active_user_per_minute=options["requests_per_minute"],
            measured_rps_per_api_pod=rps,
            measured_jobs_per_worker_second=jobs,
            jobs_per_active_user_per_hour=DEFAULT_JOBS_PER_USER_HOUR,
            events_per_request=options["events_per_request"],
            measured_events_per_partition_second=events or 50.0,
            headroom=options["headroom"],
        )
        budget = connection_budget("production")
        api_ceiling = next(c["maxReplicas"] for c in budget["components"] if c["deployment"] == "jt-code-api")
        result["productionCeilings"] = {"apiMaxReplicas": api_ceiling}
        result["gaps"] = (
            [f"API needs {result['apiPods']} pods but the production HPA allows {api_ceiling}"]
            if result["apiPods"] > api_ceiling
            else []
        )
        self.stdout.write(render_markdown(result) if options["format"] == "markdown" else dumps(result))
