from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.events.contracts import KNOWN_EVENT_TYPES
from apps.events.kafka import kafka_client_config
from apps.events.outbox import topic_name


def desired_topics() -> list[str]:
    return sorted(topic_name(event_type) for event_type in KNOWN_EVENT_TYPES)


class Command(BaseCommand):
    help = "Create any missing JT-Code Kafka topics with explicit partitions, replication and retention."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run", action="store_true", help="List missing topics without creating them."
        )

    def handle(self, *args, **options) -> None:
        from confluent_kafka.admin import AdminClient, NewTopic

        admin = AdminClient(kafka_client_config())
        try:
            existing = set(admin.list_topics(timeout=10).topics)
        except Exception as exc:  # noqa: BLE001 - surface broker/auth failures clearly
            raise CommandError(f"Could not list Kafka topics: {exc}") from exc
        missing = [topic for topic in desired_topics() if topic not in existing]
        if not missing:
            self.stdout.write(self.style.SUCCESS("All JT-Code topics exist."))
            return
        if options["dry_run"]:
            for topic in missing:
                self.stdout.write(f"missing {topic}")
            return
        new_topics = [
            NewTopic(
                topic,
                num_partitions=settings.KAFKA_TOPIC_PARTITIONS,
                replication_factor=settings.KAFKA_TOPIC_REPLICATION_FACTOR,
                config={"retention.ms": str(settings.KAFKA_TOPIC_RETENTION_MS), "cleanup.policy": "delete"},
            )
            for topic in missing
        ]
        failures = []
        for topic, future in admin.create_topics(new_topics, request_timeout=30).items():
            try:
                future.result()
                self.stdout.write(f"created {topic}")
            except Exception as exc:  # noqa: BLE001 - report every topic before failing
                failures.append(f"{topic}: {exc}")
        if failures:
            raise CommandError("Failed to create topics:\n" + "\n".join(failures))
