from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.events.kafka import kafka_client_config
from apps.events.topics import desired_topics, inspect_topics, partitions_for, topic_config

__all__ = ["Command", "desired_topics", "partitions_for"]


class Command(BaseCommand):
    help = (
        "Create missing JT-Code Kafka topics and reconcile topic settings (retention, cleanup policy, "
        "min.insync.replicas). Fails if an existing topic has fewer replicas than configured."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Report changes without applying them.")
        parser.add_argument(
            "--grow",
            action="store_true",
            help="Also add partitions to existing topics below their configured count (never shrinks).",
        )

    def handle(self, *args, **options) -> None:
        from confluent_kafka.admin import AdminClient

        dry_run = options["dry_run"]
        admin = AdminClient(kafka_client_config())
        try:
            metadata = admin.list_topics(timeout=10).topics
        except Exception as exc:  # noqa: BLE001 - surface broker/auth failures clearly
            raise CommandError(f"Could not list Kafka topics: {exc}") from exc
        report = inspect_topics(admin, metadata)
        failures: list[str] = []
        if options["grow"]:
            failures += self._grow(admin, report.under_partitioned, dry_run=dry_run)
        failures += self._create(admin, report.missing, dry_run=dry_run)
        failures += self._reconcile(admin, report.config_drift, dry_run=dry_run)
        for topic in report.unreadable_config:
            self.stderr.write(
                f"warning: could not read the configuration of {topic}; settings not reconciled"
            )
        # Replication factor cannot be raised in place; it needs a partition reassignment by an operator.
        failures += [
            f"{topic}: replication factor {have} is below KAFKA_TOPIC_REPLICATION_FACTOR={want}"
            for topic, (have, want) in report.under_replicated.items()
        ]
        if failures:
            raise CommandError("Kafka topic provisioning failed:\n" + "\n".join(failures))
        if not (report.missing or report.config_drift or (options["grow"] and report.under_partitioned)):
            self.stdout.write(self.style.SUCCESS("All JT-Code topics exist with the configured settings."))

    def _create(self, admin, missing: list[str], *, dry_run: bool) -> list[str]:
        from confluent_kafka.admin import NewTopic

        if dry_run:
            for topic in missing:
                self.stdout.write(f"missing {topic}")
            return []
        if not missing:
            return []
        new_topics = [
            NewTopic(
                topic,
                num_partitions=partitions_for(topic),
                replication_factor=settings.KAFKA_TOPIC_REPLICATION_FACTOR,
                config=topic_config(),
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
        return failures

    def _reconcile(
        self, admin, drift: dict[str, dict[str, tuple[str | None, str]]], *, dry_run: bool
    ) -> list[str]:
        from confluent_kafka.admin import AlterConfigOpType, ConfigEntry, ConfigResource

        for topic, changes in drift.items():
            for key, (have, want) in changes.items():
                self.stdout.write(f"{'would set' if dry_run else 'set'} {topic} {key}: {have!r} -> {want!r}")
        if dry_run or not drift:
            return []
        resources = [
            ConfigResource(
                ConfigResource.Type.TOPIC,
                topic,
                incremental_configs=[
                    ConfigEntry(key, want, incremental_operation=AlterConfigOpType.SET)
                    for key, (_have, want) in changes.items()
                ],
            )
            for topic, changes in drift.items()
        ]
        failures = []
        for resource, future in admin.incremental_alter_configs(resources, request_timeout=30).items():
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - report every topic before failing
                failures.append(f"{resource.name}: could not update settings: {exc}")
        return failures

    def _grow(self, admin, under: dict[str, tuple[int, int]], *, dry_run: bool) -> list[str]:
        from confluent_kafka.admin import NewPartitions

        for topic, (have, want) in under.items():
            self.stdout.write(f"grow {topic}: {have} -> {want}")
        if dry_run or not under:
            return []
        failures = []
        requests = [NewPartitions(topic, want) for topic, (_have, want) in under.items()]
        for topic, future in admin.create_partitions(requests, request_timeout=30).items():
            try:
                future.result()
                self.stdout.write(f"grew {topic}")
            except Exception as exc:  # noqa: BLE001 - report every topic before failing
                failures.append(f"{topic}: could not add partitions: {exc}")
        return failures
