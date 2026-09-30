"""Canonical queue-depth metrics derived from durable job state."""

from __future__ import annotations

from django.db.models import Count

from apps.jobs.models import Job


def queue_depths() -> list[dict[str, int | str]]:
    rows = (
        Job.objects.values("queue_name", "status")
        .filter(
            status__in=[
                Job.Status.QUEUED,
                Job.Status.VALIDATING,
                Job.Status.RUNNING,
                Job.Status.WAITING_APPROVAL,
            ]
        )
        .annotate(count=Count("id"))
        .order_by("queue_name", "status")
    )
    queues: dict[str, dict[str, int | str]] = {}
    for row in rows:
        queue = queues.setdefault(
            str(row["queue_name"]),
            {"queue": str(row["queue_name"]), "queued": 0, "running": 0, "waitingApproval": 0},
        )
        if row["status"] == Job.Status.QUEUED:
            queue["queued"] = int(row["count"])
        elif row["status"] == Job.Status.WAITING_APPROVAL:
            queue["waitingApproval"] = int(row["count"])
        else:
            queue["running"] = int(queue["running"]) + int(row["count"])
    return list(queues.values())
