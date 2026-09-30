"""Versioned, transport-neutral contracts for JT-Code domain events."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.utils import timezone

from apps.core.context import request_id_var, trace_id_var

SCHEMA_VERSION = 1


class EventContractError(ValueError):
    """An incoming event did not satisfy the supported contract."""


@dataclass(frozen=True)
class EventEnvelope:
    """The stable envelope around every Kafka event payload."""

    event_id: str
    event_type: str
    schema_version: int
    occurred_at: str
    data: dict[str, Any]
    request_id: str
    trace_id: str
    causation_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "occurred_at": self.occurred_at,
            "data": self.data,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "causation_id": self.causation_id,
        }


def event_type_for_topic(topic: str, topic_prefix: str) -> str:
    prefix = f"{topic_prefix}."
    return topic[len(prefix) :] if topic.startswith(prefix) else topic


def build_envelope(
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
    headers: dict[str, Any] | None = None,
) -> EventEnvelope:
    headers = headers or {}
    if not isinstance(payload, dict):
        raise EventContractError("Event payload must be an object.")
    request_id = str(headers.get("request_id") or request_id_var.get() or "")
    trace_id = str(headers.get("trace_id") or trace_id_var.get() or "")
    return EventEnvelope(
        event_id=event_id,
        event_type=event_type,
        schema_version=SCHEMA_VERSION,
        occurred_at=timezone.now().isoformat(),
        data=payload,
        request_id=request_id,
        trace_id=trace_id,
        causation_id=str(headers.get("causation_id") or ""),
    )


def parse_envelope(value: dict[str, Any]) -> EventEnvelope:
    """Validate and deserialize a Kafka value without accepting ambiguous shapes."""
    required = {
        "event_id",
        "event_type",
        "schema_version",
        "occurred_at",
        "data",
        "request_id",
        "trace_id",
    }
    missing = required.difference(value)
    if missing:
        raise EventContractError(f"Event envelope is missing required fields: {sorted(missing)}")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise EventContractError(f"Unsupported event schema version: {value['schema_version']!r}")
    if not isinstance(value["data"], dict):
        raise EventContractError("Event envelope data must be an object.")
    try:
        event_id = str(uuid.UUID(str(value["event_id"])))
        occurred_at = datetime.fromisoformat(str(value["occurred_at"]).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise EventContractError("Event envelope has invalid event_id or occurred_at.") from exc
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise EventContractError("Event envelope occurred_at must include a timezone offset.")
    if (
        not isinstance(value["event_type"], str)
        or not value["event_type"].strip()
        or value["event_type"] != value["event_type"].strip()
    ):
        raise EventContractError("Event envelope event_type must be a non-empty string.")
    for field in ("request_id", "trace_id", "causation_id"):
        if field in value and not isinstance(value[field], str):
            raise EventContractError(f"Event envelope {field} must be a string.")
    return EventEnvelope(
        event_id=event_id,
        event_type=value["event_type"],
        schema_version=value["schema_version"],
        occurred_at=str(value["occurred_at"]),
        data=value["data"],
        request_id=str(value["request_id"]),
        trace_id=str(value["trace_id"]),
        causation_id=str(value.get("causation_id") or ""),
    )


def transport_headers(envelope: EventEnvelope, headers: dict[str, Any] | None = None) -> dict[str, str]:
    """Provide searchable Kafka headers while retaining the full envelope in the value."""
    base = {
        "event_id": envelope.event_id,
        "event_type": envelope.event_type,
        "schema_version": str(envelope.schema_version),
        "request_id": envelope.request_id,
        "trace_id": envelope.trace_id,
        "causation_id": envelope.causation_id,
    }
    return {**{str(key): str(value) for key, value in (headers or {}).items()}, **base}


def validate_transport_headers(envelope: EventEnvelope, headers: dict[str, str]) -> None:
    """Reject a record whose searchable headers disagree with its value envelope."""
    expected = {
        "event_id": envelope.event_id,
        "event_type": envelope.event_type,
        "schema_version": str(envelope.schema_version),
        "request_id": envelope.request_id,
        "trace_id": envelope.trace_id,
        "causation_id": envelope.causation_id,
    }
    missing = [name for name in expected if name not in headers]
    if missing:
        raise EventContractError(f"Kafka event is missing required transport headers: {missing}")
    mismatched = [name for name, value in expected.items() if headers[name] != value]
    if mismatched:
        raise EventContractError(f"Kafka event transport headers disagree with envelope: {mismatched}")
