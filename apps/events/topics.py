"""Desired Kafka topic layout and checks of a live cluster against it.

Shared by ``ensure_kafka_topics`` (which creates and reconciles) and
``verify_messaging`` (which only reports), so both agree on what "correct" means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from apps.events.contracts import KNOWN_EVENT_TYPES
from apps.events.outbox import topic_name


def desired_topics() -> list[str]:
    return sorted(topic_name(event_type) for event_type in KNOWN_EVENT_TYPES)


def partitions_for(topic: str) -> int:
    """``KAFKA_TOPIC_PARTITIONS`` unless a hot topic has an override (capacity plan)."""
    prefix = f"{settings.KAFKA_TOPIC_PREFIX}."
    event_type = topic[len(prefix) :] if topic.startswith(prefix) else topic
    return int(settings.KAFKA_TOPIC_PARTITIONS_OVERRIDES.get(event_type, settings.KAFKA_TOPIC_PARTITIONS))


def topic_config() -> dict[str, str]:
    """Topic-level settings every JT-Code topic must carry."""
    config = {"retention.ms": str(settings.KAFKA_TOPIC_RETENTION_MS), "cleanup.policy": "delete"}
    # 1 is every broker's default; omitting it keeps single-broker development clusters
    # (Redpanda, which does not expose the property) working unchanged.
    if settings.KAFKA_TOPIC_MIN_INSYNC_REPLICAS > 1:
        config["min.insync.replicas"] = str(settings.KAFKA_TOPIC_MIN_INSYNC_REPLICAS)
    return config


@dataclass
class TopicReport:
    missing: list[str] = field(default_factory=list)
    under_partitioned: dict[str, tuple[int, int]] = field(default_factory=dict)
    under_replicated: dict[str, tuple[int, int]] = field(default_factory=dict)
    config_drift: dict[str, dict[str, tuple[str | None, str]]] = field(default_factory=dict)
    unreadable_config: list[str] = field(default_factory=list)

    def problems(self) -> list[str]:
        items = [f"missing topic {topic}" for topic in self.missing]
        items += [
            f"{t} has {have} partitions, needs {want}" for t, (have, want) in self.under_partitioned.items()
        ]
        items += [
            f"{t} has replication factor {have}, needs {want}"
            for t, (have, want) in self.under_replicated.items()
        ]
        items += [
            f"{t} {key}={have!r}, needs {want!r}"
            for t, drift in self.config_drift.items()
            for key, (have, want) in drift.items()
        ]
        return items


def inspect_topics(admin: Any, metadata: dict[str, Any]) -> TopicReport:
    """Compare existing topics with the desired layout; never changes the cluster."""
    from confluent_kafka.admin import ConfigResource  # type: ignore[attr-defined]

    report = TopicReport()
    present = []
    for topic in desired_topics():
        if topic not in metadata:
            report.missing.append(topic)
            continue
        present.append(topic)
        partitions = metadata[topic].partitions
        if len(partitions) < partitions_for(topic):
            report.under_partitioned[topic] = (len(partitions), partitions_for(topic))
        replicas = min((len(p.replicas) for p in partitions.values()), default=0)
        if replicas < settings.KAFKA_TOPIC_REPLICATION_FACTOR:
            report.under_replicated[topic] = (replicas, settings.KAFKA_TOPIC_REPLICATION_FACTOR)
    if not present:
        return report
    wanted = topic_config()
    resources = [ConfigResource(ConfigResource.Type.TOPIC, topic) for topic in present]
    for resource, future in admin.describe_configs(resources, request_timeout=30).items():
        try:
            entries = future.result()
        except Exception:  # noqa: BLE001 - reported, not fatal: some providers restrict DescribeConfigs
            report.unreadable_config.append(resource.name)
            continue
        drift = {
            key: (entries[key].value if key in entries else None, value)
            for key, value in wanted.items()
            if key not in entries or entries[key].value != value
        }
        if drift:
            report.config_drift[resource.name] = drift
    return report
