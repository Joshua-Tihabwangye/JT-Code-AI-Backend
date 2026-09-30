"""Redis-backed status notifications for authenticated chat SSE streams."""

from __future__ import annotations

import logging

import sentry_sdk
from django.conf import settings

logger = logging.getLogger(__name__)


def chat_status_channel(chat_request_id: str) -> str:
    return f"jt-code:chat-status:{chat_request_id}"


def notify_chat_status(chat_request_id: str) -> None:
    """Wake SSE subscribers after the surrounding database transaction commits.

    The database remains authoritative. A missed Redis notification is repaired
    by the stream's bounded reconciliation query.
    """
    if not settings.REDIS_URL:
        return
    client = None
    try:
        from redis import Redis

        client = Redis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=3,
            socket_timeout=3,
            decode_responses=True,
        )
        client.publish(chat_status_channel(chat_request_id), chat_request_id)
    except Exception as exc:  # noqa: BLE001 - notification loss must not roll back durable state
        sentry_sdk.capture_exception(exc)
        logger.warning("chat status notification failed", extra={"chat_request_id": chat_request_id})
    finally:
        if client is not None:
            client.close()
