"""Validated cron scheduling for knowledge source synchronization."""

from __future__ import annotations

from celery.schedules import crontab
from django.utils import timezone

_PRESETS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}


def parse_sync_schedule(value: str):
    """Return a Celery crontab for a standard five-field cron expression."""
    expression = _PRESETS.get((value or "").strip(), (value or "").strip())
    if not expression:
        return None
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("sync_schedule must be a five-field cron expression or a supported preset.")
    minute, hour, day_of_month, month_of_year, day_of_week = fields
    try:
        return crontab(
            minute=minute,
            hour=hour,
            day_of_week=day_of_week,
            day_of_month=day_of_month,
            month_of_year=month_of_year,
            nowfun=timezone.now,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid sync_schedule: {exc}") from exc


def source_is_due(source) -> bool:
    """Pending sources run once; indexed sources run only on their cron."""
    if source.status == source.Status.PENDING:
        return True
    schedule = parse_sync_schedule(source.sync_schedule)
    if schedule is None or source.last_synced_at is None:
        return False
    return bool(schedule.is_due(source.last_synced_at).is_due)
