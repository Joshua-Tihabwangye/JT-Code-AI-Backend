"""Celery tasks for outbound webhooks (see ``apps.integrations.outbound``)."""

from __future__ import annotations

from celery import shared_task

from apps.integrations import outbound


@shared_task(acks_late=True, ignore_result=True)
def deliver_webhook(delivery_id: str) -> str:
    return outbound.deliver(delivery_id)


@shared_task(ignore_result=True)
def sweep_webhook_deliveries() -> int:
    """Re-dispatch deliveries that are due (lost broker messages, expired leases, scheduled retries)."""
    due = outbound.due_delivery_ids()
    for delivery_id in due:
        deliver_webhook.apply_async(args=[delivery_id])
    return len(due)
