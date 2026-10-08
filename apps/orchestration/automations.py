"""Tenant automations: cron-scheduled n8n workflows, each run a metered Django job.

Inputs are validated in Django before anything reaches n8n:

* ``webhook`` - POST a JSON payload to an approved HTTPS host (``WEBHOOK_ALLOWED_HOSTS``);
* ``slack_message`` - post text to a Slack channel id;
* ``email`` - send text to members of the automation's organization only (no
  arbitrary recipients, so automations cannot be used to send spam).
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.jobs.models import Job

ACTIONS = ("webhook", "slack_message", "email")
_CHANNEL = re.compile(r"^[CGD][A-Z0-9]{6,20}$")
MAX_PAYLOAD_BYTES = 64 * 1024


def validate_input(value: Any, organization: Any) -> dict[str, Any]:
    from apps.jobs.webhooks import validate_callback_url

    if not isinstance(value, dict):
        raise ValidationError({"input": "input must be an object."})
    action = value.get("action")
    if action not in ACTIONS:
        raise ValidationError({"input": f"action must be one of {list(ACTIONS)}."})
    if action == "webhook":
        payload = value.get("payload", {})
        if not isinstance(payload, dict) or len(json.dumps(payload)) > MAX_PAYLOAD_BYTES:
            raise ValidationError({"input": "payload must be an object of at most 64 KB."})
        return {
            "action": action,
            "url": validate_callback_url(str(value.get("url") or "")),
            "payload": payload,
        }
    text = str(value.get("text") or "").strip()
    if not text or len(text) > 4000:
        raise ValidationError({"input": "text is required (at most 4000 characters)."})
    if action == "slack_message":
        channel = str(value.get("channel") or "")
        if not _CHANNEL.fullmatch(channel):
            raise ValidationError({"input": "channel must be a Slack channel id (e.g. C0123456789)."})
        return {"action": action, "channel": channel, "text": text}
    recipients = value.get("to")
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 20:
        raise ValidationError({"input": "to must list 1-20 e-mail addresses."})
    from apps.identity.models import User

    members = {
        email.lower()
        for email in User.objects.filter(organizations=organization, is_active=True).values_list(
            "email", flat=True
        )
        if email
    }
    normalized = [str(item).strip().lower() for item in recipients]
    if any(email not in members for email in normalized):
        raise ValidationError({"input": "Automations can only e-mail members of this organization."})
    subject = str(value.get("subject") or "").strip()
    if not subject or len(subject) > 200:
        raise ValidationError({"input": "subject is required (at most 200 characters)."})
    return {"action": action, "to": normalized, "subject": subject, "text": text}


def validate_schedule(value: str) -> str:
    from apps.knowledge.scheduling import parse_sync_schedule

    try:
        schedule = parse_sync_schedule(value)
    except ValueError as exc:
        raise ValidationError({"schedule": str(exc)}) from exc
    if schedule is None:
        raise ValidationError({"schedule": "A five-field cron expression or preset is required."})
    if str(value).split()[0] == "*":
        raise ValidationError({"schedule": "Use a fixed minute; automations may not run every minute."})
    return value


def next_run_after(schedule: str, moment: Any) -> Any:
    from apps.knowledge.scheduling import parse_sync_schedule

    parsed = parse_sync_schedule(schedule)
    remaining = parsed.remaining_estimate(moment)
    return moment + max(remaining, timedelta(seconds=60))


def start_automation_run(automation: Any, *, manual: bool = False) -> Job:
    """Create, meter and dispatch one job for ``automation`` (raises on quota/credit errors)."""
    from apps.jobs.dispatch import enqueue_job
    from apps.jobs.services import reserve_job_credits

    with transaction.atomic():
        job = Job.objects.create(
            owner=automation.created_by,
            organization=automation.organization,
            task_type=Job.TaskType.SCHEDULED_AUTOMATION,
            input_payload={**automation.input, "automationId": str(automation.id), "manual": manual},
            idempotency_key=f"automation:{automation.id}:{timezone.now().isoformat()}",
        )
        reserve_job_credits(job, automation.created_by)
        enqueue_job(job)
        automation.last_job = job
        automation.last_run_at = timezone.now()
        automation.run_count += 1
        automation.last_error = ""
        automation.save(update_fields=["last_job", "last_run_at", "run_count", "last_error", "updated_at"])
    return job


def run_due_automations(now: Any = None) -> int:
    from apps.orchestration.models import Automation

    now = now or timezone.now()
    started = 0
    due_ids = list(
        Automation.objects.filter(is_active=True, next_run_at__lte=now).values_list("id", flat=True)[:100]
    )
    for automation_id in due_ids:
        with transaction.atomic():
            automation = (
                Automation.objects.select_for_update(skip_locked=True, of=("self",))
                .select_related("organization", "created_by")
                .filter(id=automation_id, is_active=True, next_run_at__lte=now)
                .first()
            )
            if automation is None:
                continue
            # Advance the schedule first, so a failure never re-runs the same slot.
            automation.next_run_at = next_run_after(automation.schedule, now)
            automation.save(update_fields=["next_run_at", "updated_at"])
        if automation.created_by is None or not automation.created_by.is_active:
            automation.is_active = False
            automation.last_error = "The automation's owner is no longer active."
            automation.save(update_fields=["is_active", "last_error", "updated_at"])
            continue
        try:
            start_automation_run(automation)
        except Exception as exc:  # noqa: BLE001 - recorded on the automation (quota, credits, n8n)
            automation.last_error = str(getattr(exc, "detail", exc))[:2000]
            automation.save(update_fields=["last_error", "updated_at"])
            continue
        started += 1
    return started
