"""Verify the live Redis and Kafka services match what this application needs.

Settings validation can only check URLs. This command connects and checks what
the services are actually configured to do: Redis eviction policies and role
separation, and the Kafka topic layout. It runs at deploy time (the ``migrate``
role); with ``--strict`` any problem fails the deploy.
"""

from __future__ import annotations

from typing import Any, cast

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

# Policies that let a cache shed keys under memory pressure.
EVICTING_POLICIES = frozenset({"allkeys-lru", "allkeys-lfu", "volatile-lru", "volatile-lfu", "volatile-ttl"})


class Command(BaseCommand):
    help = "Check Redis (cache and Celery broker) and Kafka against production requirements."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--strict", action="store_true", help="Exit non-zero on any problem.")
        parser.add_argument("--skip-kafka", action="store_true")
        parser.add_argument("--skip-redis", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        problems: list[str] = []
        warnings: list[str] = []
        if not options["skip_redis"]:
            self._check_redis(problems, warnings)
        if not options["skip_kafka"]:
            self._check_kafka(problems, warnings)
        for item in warnings:
            self.stderr.write(f"warning: {item}")
        if problems and options["strict"]:
            raise CommandError("Messaging verification failed:\n" + "\n".join(f"- {p}" for p in problems))
        for item in problems:
            self.stderr.write(f"problem: {item}")
        if not problems:
            self.stdout.write(self.style.SUCCESS("Redis and Kafka meet the production requirements."))

    # Redis --------------------------------------------------------------------------------

    @staticmethod
    def _redis_info(url: str) -> dict[str, Any]:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=5, socket_timeout=5)
        try:
            client.ping()
            info: dict[str, Any] = {}
            for section in ("server", "memory", "persistence"):
                info.update(cast(dict[str, Any], client.info(section)))
            return info
        finally:
            client.close()

    def _check_redis(self, problems: list[str], warnings: list[str]) -> None:
        roles = {
            "cache (REDIS_URL)": settings.REDIS_URL,
            "broker (CELERY_BROKER_URL)": settings.CELERY_BROKER_URL,
            "results (CELERY_RESULT_BACKEND)": settings.CELERY_RESULT_BACKEND,
        }
        info: dict[str, dict[str, Any]] = {}
        for role, url in roles.items():
            if not url:
                problems.append(f"Redis {role} is not configured.")
                continue
            try:
                info[role] = self._redis_info(url)
            except Exception as exc:  # noqa: BLE001 - report the role, never the URL (it holds a password)
                problems.append(f"Redis {role} is unreachable: {type(exc).__name__}: {exc}")
        cache, broker = info.get("cache (REDIS_URL)"), info.get("broker (CELERY_BROKER_URL)")
        if cache:
            policy = cache.get("maxmemory_policy")
            if policy is None:
                warnings.append(
                    "Redis cache maxmemory-policy is not reported by INFO; confirm it evicts (allkeys-lru)."
                )
            elif policy not in EVICTING_POLICIES:
                problems.append(
                    f"Redis cache maxmemory-policy is {policy!r}; use allkeys-lru so a full cache evicts."
                )
            if not cache.get("maxmemory"):
                warnings.append("Redis cache has no maxmemory limit; set one so eviction can take effect.")
        if broker:
            policy = broker.get("maxmemory_policy")
            if policy is None:
                warnings.append(
                    "Redis broker maxmemory-policy is not reported by INFO; confirm it is noeviction."
                )
            elif policy != "noeviction":
                problems.append(
                    f"Redis broker maxmemory-policy is {policy!r}; it must be noeviction "
                    "or queued jobs can be lost."
                )
            if str(broker.get("aof_enabled", "0")) != "1":
                warnings.append(
                    "Redis broker has AOF persistence off; confirm the provider replicates or snapshots it "
                    "(jobs are re-dispatched from PostgreSQL after a loss, but in-flight work is redone)."
                )
        if cache and broker and cache.get("run_id") and cache.get("run_id") == broker.get("run_id"):
            problems.append(
                "REDIS_URL and CELERY_BROKER_URL reach the same Redis server; use separate instances."
            )

    # Kafka --------------------------------------------------------------------------------

    def _check_kafka(self, problems: list[str], warnings: list[str]) -> None:
        from confluent_kafka.admin import AdminClient, ConfigResource  # type: ignore[attr-defined]

        from apps.events.kafka import kafka_client_config
        from apps.events.topics import inspect_topics

        if not settings.KAFKA_BOOTSTRAP_SERVERS:
            problems.append("KAFKA_BOOTSTRAP_SERVERS is not configured.")
            return
        admin = AdminClient(kafka_client_config())
        try:
            metadata = admin.list_topics(timeout=10)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"Kafka is unreachable: {exc}")
            return
        brokers = len(metadata.brokers)
        if brokers < settings.KAFKA_TOPIC_REPLICATION_FACTOR:
            problems.append(
                f"Kafka has {brokers} broker(s), fewer than KAFKA_TOPIC_REPLICATION_FACTOR="
                f"{settings.KAFKA_TOPIC_REPLICATION_FACTOR}."
            )
        report = inspect_topics(admin, metadata.topics)
        problems += report.problems()
        warnings += [f"could not read the configuration of topic {t}" for t in report.unreadable_config]
        broker_id = str(next(iter(metadata.brokers), ""))
        if not broker_id:
            return
        try:
            resource = ConfigResource(ConfigResource.Type.BROKER, broker_id)
            entries = admin.describe_configs([resource], request_timeout=15)[resource].result()
        except Exception:  # noqa: BLE001 - managed providers often restrict broker configs
            warnings.append("could not read broker configuration; confirm auto.create.topics.enable=false.")
            return
        auto_create = entries.get("auto.create.topics.enable")
        if auto_create is not None and str(auto_create.value).lower() == "true":
            problems.append(
                "Kafka auto.create.topics.enable is true; a mistyped topic would be created silently. "
                "Topics are provisioned by ensure_kafka_topics."
            )
