"""Concrete, idempotent Phase 6 consumer handlers owned by the integrations domain."""

from __future__ import annotations

from django.utils import timezone

from apps.events.consumers import register_handler
from apps.events.contracts import EventContractError, EventEnvelope
from apps.integrations.models import WebhookDelivery


@register_handler("integrations.webhook.received")
def mark_incoming_webhook_processed(envelope: EventEnvelope) -> None:
    """Durably acknowledge a verified inbound webhook after Kafka consumption.

    The HTTP endpoint persists the original payload before publishing this event.
    This handler only changes the matching delivery record, verifies the envelope
    cannot be redirected to a different webhook, and relies on ``ConsumedEvent``
    plus the surrounding transaction for exactly-once local effects.
    """
    delivery_id = envelope.data.get("delivery_id")
    webhook_id = envelope.data.get("webhook_id")
    if not isinstance(delivery_id, str) or not isinstance(webhook_id, str):
        raise EventContractError("integrations.webhook.received requires delivery_id and webhook_id.")
    try:
        delivery = WebhookDelivery.objects.select_for_update().get(id=delivery_id)
    except (WebhookDelivery.DoesNotExist, ValueError) as exc:
        raise EventContractError("Referenced webhook delivery does not exist.") from exc
    if str(delivery.webhook_id) != webhook_id:
        raise EventContractError("Webhook delivery does not belong to the envelope webhook_id.")
    if delivery.status == WebhookDelivery.Status.DELIVERED:
        return
    if delivery.status != WebhookDelivery.Status.PENDING:
        raise EventContractError(f"Webhook delivery is not processable from state {delivery.status!r}.")
    delivery.status = WebhookDelivery.Status.DELIVERED
    delivery.response_status = 202
    delivery.response_body = "Accepted by JT-Code event consumer."
    delivery.delivered_at = timezone.now()
    delivery.error_message = ""
    delivery.save(
        update_fields=(
            "status",
            "response_status",
            "response_body",
            "delivered_at",
            "error_message",
        )
    )
