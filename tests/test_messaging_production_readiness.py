"""Production readiness of Redis and Kafka: topic durability, deploy-time verification,
role separation, consumer observability and the deployment manifests that enforce them."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.events.topics import desired_topics, topic_config

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "infra" / "k8s"


@pytest.fixture
def kafka_settings(settings):
    settings.KAFKA_BOOTSTRAP_SERVERS = "kafka.example.net:9093"
    settings.KAFKA_TOPIC_PREFIX = "jt-code.production"
    settings.KAFKA_TOPIC_PARTITIONS = 3
    settings.KAFKA_TOPIC_PARTITIONS_OVERRIDES = {}
    settings.KAFKA_TOPIC_REPLICATION_FACTOR = 3
    settings.KAFKA_TOPIC_MIN_INSYNC_REPLICAS = 2
    settings.KAFKA_TOPIC_RETENTION_MS = 604800000
    return settings


class _Future:
    def __init__(self, value=None, error=None):
        self._value, self._error = value, error

    def result(self):
        if self._error:
            raise self._error
        return self._value


def _entries(values: dict[str, str]):
    return {key: SimpleNamespace(value=value) for key, value in values.items()}


def _topic(replicas: int = 3, partitions: int = 3):
    return SimpleNamespace(
        partitions={p: SimpleNamespace(replicas=list(range(replicas))) for p in range(partitions)}
    )


class FakeAdmin:
    """The parts of confluent_kafka's AdminClient the commands use."""

    def __init__(self, topics, configs=None, *, brokers=3, broker_config=None):
        self.topics, self.configs = topics, configs or {}
        self.brokers, self.broker_config = brokers, broker_config
        self.created, self.altered = [], []

    def list_topics(self, timeout):
        return SimpleNamespace(brokers={i: object() for i in range(self.brokers)}, topics=self.topics)

    def describe_configs(self, resources, request_timeout):
        result = {}
        for resource in resources:
            if resource.name in self.topics:
                result[resource] = _Future(_entries(self.configs.get(resource.name, topic_config())))
            elif self.broker_config is None:
                result[resource] = _Future(error=RuntimeError("authorization failed"))
            else:
                result[resource] = _Future(_entries(self.broker_config))
        return result

    def create_topics(self, new_topics, request_timeout):
        self.created += new_topics
        return {topic.topic: _Future() for topic in new_topics}

    def incremental_alter_configs(self, resources, request_timeout):
        self.altered += resources
        return {resource: _Future() for resource in resources}

    def create_partitions(self, requests, request_timeout):
        return {request.topic: _Future() for request in requests}


def _install(monkeypatch, admin):
    monkeypatch.setattr("confluent_kafka.admin.AdminClient", lambda config: admin)


def _all_topics(**overrides):
    return {topic: overrides.get(topic, _topic()) for topic in desired_topics()}


# Topic provisioning ---------------------------------------------------------------------------


def test_missing_topics_are_created_with_replication_and_min_insync_replicas(kafka_settings, monkeypatch):
    admin = FakeAdmin({})
    _install(monkeypatch, admin)

    call_command("ensure_kafka_topics")

    assert {t.topic for t in admin.created} == set(desired_topics())
    assert {t.replication_factor for t in admin.created} == {3}
    assert all(t.config["min.insync.replicas"] == "2" for t in admin.created)
    assert all(t.config["cleanup.policy"] == "delete" for t in admin.created)


def test_existing_topic_settings_are_reconciled(kafka_settings, monkeypatch):
    drifted = desired_topics()[0]
    admin = FakeAdmin(_all_topics(), {drifted: {"retention.ms": "3600000", "cleanup.policy": "delete"}})
    _install(monkeypatch, admin)

    call_command("ensure_kafka_topics")

    assert [resource.name for resource in admin.altered] == [drifted]
    changes = {entry.name: entry.value for entry in admin.altered[0].incremental_configs}
    assert changes == {"retention.ms": "604800000", "min.insync.replicas": "2"}


def test_under_replicated_topic_fails_provisioning(kafka_settings, monkeypatch):
    weak = desired_topics()[1]
    _install(monkeypatch, FakeAdmin(_all_topics(**{weak: _topic(replicas=1)})))

    with pytest.raises(CommandError, match="replication factor 1"):
        call_command("ensure_kafka_topics")


def test_single_broker_development_does_not_send_min_insync_replicas(kafka_settings):
    kafka_settings.KAFKA_TOPIC_MIN_INSYNC_REPLICAS = 1
    assert "min.insync.replicas" not in topic_config()


# Deploy-time verification ---------------------------------------------------------------------

_HEALTHY_REDIS = {
    "redis://cache": {"run_id": "cache", "maxmemory_policy": "allkeys-lru", "maxmemory": 1 << 30},
    "redis://broker/1": {"run_id": "broker", "maxmemory_policy": "noeviction", "aof_enabled": 1},
    "redis://broker/2": {"run_id": "broker", "maxmemory_policy": "noeviction", "aof_enabled": 1},
}


@pytest.fixture
def redis_settings(settings):
    settings.REDIS_URL = "redis://cache"
    settings.CELERY_BROKER_URL = "redis://broker/1"
    settings.CELERY_RESULT_BACKEND = "redis://broker/2"
    return settings


def _redis(monkeypatch, servers):
    from apps.core.management.commands import verify_messaging

    monkeypatch.setattr(verify_messaging.Command, "_redis_info", staticmethod(lambda url: dict(servers[url])))


def test_verify_messaging_passes_for_a_production_shaped_setup(kafka_settings, redis_settings, monkeypatch):
    _redis(monkeypatch, _HEALTHY_REDIS)
    _install(monkeypatch, FakeAdmin(_all_topics(), broker_config={"auto.create.topics.enable": "false"}))

    call_command("verify_messaging", "--strict")


def test_verify_messaging_rejects_shared_or_evicting_broker_and_kafka_gaps(
    kafka_settings, redis_settings, monkeypatch
):
    shared = {
        "redis://cache": {"run_id": "same", "maxmemory_policy": "noeviction", "maxmemory": 0},
        "redis://broker/1": {"run_id": "same", "maxmemory_policy": "allkeys-lru"},
        "redis://broker/2": {"run_id": "same", "maxmemory_policy": "allkeys-lru"},
    }
    _redis(monkeypatch, shared)
    topics = _all_topics()
    topics.pop(desired_topics()[0])
    _install(monkeypatch, FakeAdmin(topics, brokers=1, broker_config={"auto.create.topics.enable": "true"}))

    with pytest.raises(CommandError) as failure:
        call_command("verify_messaging", "--strict")

    message = str(failure.value)
    assert "cache maxmemory-policy is 'noeviction'" in message
    assert "broker maxmemory-policy is 'allkeys-lru'" in message
    assert "reach the same Redis server" in message
    assert f"missing topic {desired_topics()[0]}" in message
    assert "1 broker(s)" in message
    assert "auto.create.topics.enable is true" in message


def test_verify_messaging_only_reports_outside_strict_mode(kafka_settings, redis_settings, monkeypatch):
    _redis(monkeypatch, {url: {"run_id": "one", "maxmemory_policy": "noeviction"} for url in _HEALTHY_REDIS})
    _install(monkeypatch, FakeAdmin({}))

    call_command("verify_messaging")  # development: problems are printed, the command succeeds


# Settings validation --------------------------------------------------------------------------


def _messaging_problems(monkeypatch, **env):
    from config.settings.validation import _check_messaging

    for key in (
        "REDIS_URL",
        "CELERY_BROKER_URL",
        "KEDA_REDIS_ADDRESS",
        "KAFKA_TOPIC_REPLICATION_FACTOR",
        "KAFKA_TOPIC_MIN_INSYNC_REPLICAS",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    problems: list[str] = []
    _check_messaging(problems, strict=True)
    return problems


def test_validation_requires_a_separate_broker_and_matching_keda_address(monkeypatch):
    problems = _messaging_problems(
        monkeypatch,
        REDIS_URL="rediss://redis.example.net:6380/0",
        CELERY_BROKER_URL="rediss://user:pw@REDIS.example.net:6380/1",  # pragma: allowlist secret
        KEDA_REDIS_ADDRESS="other.example.net:6380",
    )
    assert any("separate Redis instance" in p for p in problems)
    assert any("KEDA_REDIS_ADDRESS" in p for p in problems)

    assert (
        _messaging_problems(
            monkeypatch,
            REDIS_URL="rediss://cache.example.net:6380/0",
            CELERY_BROKER_URL="rediss://broker.example.net:6380/0",
            KEDA_REDIS_ADDRESS="broker.example.net:6380",
        )
        == []
    )


@pytest.mark.parametrize(
    ("replicas", "insync", "expected"),
    [
        ("3", "2", None),
        ("1", "1", "at least 3"),
        ("3", "1", "at least 2"),
        ("3", "3", "below the replication"),
        ("2", "3", "between 1 and"),
    ],
)
def test_validation_enforces_kafka_durability(monkeypatch, replicas, insync, expected):
    problems = _messaging_problems(
        monkeypatch, KAFKA_TOPIC_REPLICATION_FACTOR=replicas, KAFKA_TOPIC_MIN_INSYNC_REPLICAS=insync
    )
    if expected is None:
        assert problems == []
    else:
        assert any(expected in p for p in problems), problems


# Runtime behaviour ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_kafka_outage_is_reported_but_keeps_api_pods_ready(client, settings, monkeypatch):
    settings.HEALTHCHECK_EXTERNAL_DEPENDENCIES = True
    broker = SimpleNamespace(ensure_connection=lambda **_: None)
    monkeypatch.setattr("apps.core.views.current_app", SimpleNamespace(connection_for_read=lambda: broker))

    class DownAdmin:
        def __init__(self, config):
            pass

        def list_topics(self, timeout):
            raise RuntimeError("all brokers down")

    monkeypatch.setattr("confluent_kafka.admin.AdminClient", DownAdmin)

    response = client.get("/api/v1/health/ready/")

    assert response.status_code == 200
    assert response.json()["checks"]["kafka"] == "failed"


def test_consumer_reports_lag_per_assigned_partition():
    from apps.core.metrics import KAFKA_CONSUMER_LAG
    from apps.events.management.commands.run_kafka_consumer import Command

    committed = [
        SimpleNamespace(topic="jt-code.t", partition=0, offset=90),
        SimpleNamespace(topic="jt-code.t", partition=1, offset=-1001),  # nothing committed yet
    ]
    watermarks = {0: (0, 100), 1: (40, 55)}
    consumer = SimpleNamespace(
        assignment=lambda: committed,
        committed=lambda assignment, timeout: committed,
        get_watermark_offsets=lambda tp, timeout, cached: watermarks[tp.partition],
    )

    Command._record_lag(consumer, "jt-code.lag-test")

    assert KAFKA_CONSUMER_LAG.labels("jt-code.lag-test", "jt-code.t", "0")._value.get() == 10
    assert KAFKA_CONSUMER_LAG.labels("jt-code.lag-test", "jt-code.t", "1")._value.get() == 15


# Deployment artifacts -------------------------------------------------------------------------


def _docs(path: Path):
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


def test_consumer_pod_is_observable_restartable_and_uses_its_own_kafka_principal():
    (consumer,) = _docs(K8S / "base" / "consumers.yaml")
    container = consumer["spec"]["template"]["spec"]["containers"][0]
    assert container["envFrom"][-1] == {"secretRef": {"name": "jt-code-kafka-consumer"}}
    assert "kafka-consumer.heartbeat" in " ".join(container["livenessProbe"]["exec"]["command"])
    assert {"name": "metrics", "containerPort": 9808, "protocol": "TCP"} in container["ports"]

    (migrate,) = _docs(K8S / "base" / "migrate-job.yaml")
    migrate_env = migrate["spec"]["template"]["spec"]["containers"][0]["envFrom"]
    assert migrate_env[-1] == {"secretRef": {"name": "jt-code-kafka-admin"}}

    monitors = {d["metadata"]["name"]: d for d in _docs(K8S / "components" / "monitoring" / "monitors.yaml")}
    selected = monitors["jt-code-workers"]["spec"]["selector"]["matchExpressions"][0]["values"]
    assert "consumer-integrations" in selected


def test_egress_is_restricted_to_service_ports():
    policies = {p["metadata"]["name"]: p for p in _docs(K8S / "base" / "networkpolicies.yaml")}
    rules = policies["deny-cloud-metadata"]["spec"]["egress"]
    assert all(rule.get("ports") for rule in rules), "no egress rule may allow every port"
    ports = {(p["port"], p["protocol"]) for p in rules[0]["ports"]}
    assert {(443, "TCP"), (6380, "TCP"), (9093, "TCP"), (53, "UDP")} <= ports
    assert (80, "TCP") not in ports


def test_deploys_provision_topics_and_gate_on_messaging_checks():
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text()
    migrate = entrypoint.split("migrate)", 1)[1].split(";;", 1)[0]
    assert "ensure_kafka_topics --grow" in migrate
    assert "verify_messaging --strict" in migrate
    assert migrate.index("ensure_kafka_topics") < migrate.index("verify_messaging")

    # Production and the native local broker never auto-create topics; migrate provisions them.
    production = (ROOT / "docker-compose.prod.yml").read_text()
    assert "kafka-init" not in production
    assert "auto.create.topics.enable=false" in (ROOT / "infra/native/kafka.properties").read_text()


def test_terraform_creates_distinct_kafka_role_secrets():
    app = (ROOT / "infra/terraform/modules/app_environment/main.tf").read_text()
    assert 'resource "kubernetes_secret" "kafka_role"' in app
    assert 'name      = "jt-code-kafka-${each.key}"' in app
    assert "three different principals" in app
    for env in ("staging", "production"):
        assert "kafka_role_credentials" in (ROOT / f"infra/terraform/envs/{env}/main.tf").read_text()


# Single-server production (docker-compose.prod.yml) ------------------------------------------


def _strict_problems(monkeypatch, env: dict[str, str]) -> list[str]:
    import os

    from config.settings.validation import validate_environment

    for key in list(os.environ):
        if key.isupper() and not key.startswith(("PATH", "HOME", "PYTHON", "PYTEST", "VIRTUAL_ENV")):
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return validate_environment("production")


def _single_host_env() -> dict[str, str]:
    """``.env.production.example`` with dummy secrets, plus the values docker-compose.prod.yml injects."""
    import re

    from config.settings.validation import _REQUIRED_STRICT
    from tests.test_phase17_infrastructure import _DUMMY

    compose_text = (ROOT / "docker-compose.prod.yml").read_text()
    # Fill only what an operator must fill: values validation requires and values the compose
    # file refuses to start without (${VAR:?...}). Optional integrations stay empty, as in reality.
    required = {*_REQUIRED_STRICT, *re.findall(r"\$\{(\w+):\?", compose_text), "GEMINI_API_KEY"}
    required |= {"TOOL_CREDENTIALS_ENCRYPTION_KEYS"}
    env = {}
    for line in (ROOT / ".env.production.example").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if not value and key in required:
                value = _DUMMY.get(key, f"{key.lower()}-0123456789abcdefghijklmnopqrstuvwxyz")
            env[key] = value
    compose = yaml.safe_load(compose_text)

    def interpolate(value: str) -> str:
        return re.sub(r"\$\{(\w+)[^}]*\}", lambda m: env[m.group(1)], str(value))

    env.update({key: interpolate(value) for key, value in compose["x-app-env"].items()})
    return env


def test_production_template_and_compose_pass_single_host_validation(monkeypatch):
    assert _strict_problems(monkeypatch, _single_host_env()) == []


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"REDIS_URL": "redis://redis-cache:6379/0"}, "REDIS_URL must include a password"),
        ({"KAFKA_SECURITY_PROTOCOL": "PLAINTEXT"}, "SASL_PLAINTEXT or SASL_SSL"),
        ({"KAFKA_TOPIC_REPLICATION_FACTOR": "3", "KAFKA_TOPIC_MIN_INSYNC_REPLICAS": "2"}, "one Kafka broker"),
        ({"SUPABASE_JWT_SECRET": "too-short"}, "SUPABASE_JWT_SECRET must be a random secret"),
        (
            {
                "DATABASE_URL": "postgresql://postgres:pw@db.example.com:5432/postgres"
            },  # pragma: allowlist secret
            "sslmode",
        ),
        (
            {"CELERY_BROKER_URL": "redis://:pw@redis-cache:6379/1"},  # pragma: allowlist secret
            "separate Redis instance",
        ),
    ],
)
def test_single_host_mode_still_requires_auth_and_separate_redis(monkeypatch, override, expected):
    problems = _strict_problems(monkeypatch, {**_single_host_env(), **override})
    assert any(expected in problem for problem in problems), problems


def test_without_single_host_mode_production_still_demands_tls_and_replication(monkeypatch):
    env = {**_single_host_env(), "SINGLE_HOST_DEPLOYMENT": "false"}
    problems = " ".join(_strict_problems(monkeypatch, env))
    assert "rediss://" in problems
    assert "SASL_SSL" in problems
    assert "at least 3" in problems
    assert "must be jwks" in problems  # self-hosted HS256 sessions are single-host only
    assert "sslmode" in problems  # the database by service name needs TLS once it leaves the host


def test_production_compose_exposes_nothing_publicly_and_configures_each_role():
    compose = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())
    services = compose["services"]
    for name, service in services.items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} beyond localhost"
    assert "kafka" not in {name for name, s in services.items() if s.get("ports")}
    cache = " ".join(map(str, services["redis-cache"]["command"]))
    broker = " ".join(map(str, services["redis-broker"]["command"]))
    assert "allkeys-lru" in cache and "--requirepass" in cache
    assert "noeviction" in broker and "--appendonly yes" in broker and "--requirepass" in broker
    assert services["consumer"]["environment"]["KAFKA_SASL_USERNAME"] == "jt-consumer"
    assert services["migrate"]["environment"]["KAFKA_SASL_USERNAME"] == "jt-admin"
    bootstrap = yaml.safe_load((ROOT / "docker/redpanda/bootstrap.yaml").read_text())
    assert bootstrap == {"enable_sasl": True, "superusers": ["jt-admin"], "auto_create_topics_enabled": False}


def test_verify_messaging_requires_broker_persistence_on_a_single_host(
    kafka_settings, redis_settings, monkeypatch, settings
):
    settings.SINGLE_HOST_DEPLOYMENT = True
    servers = {url: dict(info) for url, info in _HEALTHY_REDIS.items()}
    for url in ("redis://broker/1", "redis://broker/2"):
        servers[url]["aof_enabled"] = 0
    _redis(monkeypatch, servers)
    _install(monkeypatch, FakeAdmin(_all_topics(), broker_config={"auto.create.topics.enable": "false"}))

    with pytest.raises(CommandError, match="appendonly yes"):
        call_command("verify_messaging", "--strict")


# Monitoring (Prometheus, Grafana, OTel) and internal API hosts ------------------------------


def test_compose_stacks_let_prometheus_and_health_checks_reach_the_api():
    compose = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())
    env = compose["x-app-env"]
    assert {"api", "127.0.0.1"} <= set(env["DJANGO_INTERNAL_HOSTS"].split(","))
    for profile in ("development", "staging", "production"):
        assert 'env_list("DJANGO_INTERNAL_HOSTS")' in (ROOT / f"config/settings/{profile}.py").read_text()


def _scrape_targets(config: str) -> set[str]:
    scrape = yaml.safe_load((ROOT / "infra/prometheus" / config).read_text())["scrape_configs"]
    return {t for job in scrape for group in job["static_configs"] for t in group["targets"]}


def test_prometheus_scrapes_every_metrics_source_in_both_stacks():
    production = {"api:8000", "worker:9808", "worker-documents:9808", "consumer:9808", "kafka:9644"}
    production |= {
        "otel-collector:8888",
        "tempo:3200",
        "redis-exporter-cache:9121",
        "redis-exporter-broker:9121",
    }
    assert production <= _scrape_targets("single-host.yml")
    # Native local stack: every process runs on the host, so each has its own loopback port.
    native = {"127.0.0.1:8000", "127.0.0.1:9808", "127.0.0.1:9809", "127.0.0.1:9121"}
    native |= {"127.0.0.1:8888", "127.0.0.1:3200"}
    assert native <= _scrape_targets("local.yml")
    production = yaml.safe_load((ROOT / "infra/prometheus/single-host.yml").read_text())["scrape_configs"]
    api = next(job for job in production if job["job_name"] == "jt-code-api")
    assert api["authorization"]["credentials_file"] == "/run/jt-code/metrics-token"


def test_production_monitoring_requires_sign_in_and_stays_on_localhost():
    services = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())["services"]
    grafana = services["grafana"]["environment"]
    assert grafana["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert grafana["GF_SECURITY_ADMIN_PASSWORD"].startswith("${GRAFANA_ADMIN_PASSWORD:?")
    for name in (
        "grafana",
        "prometheus",
        "otel-collector",
        "tempo",
        "redis-exporter-cache",
        "redis-exporter-broker",
    ):
        assert name in services
    assert "/run/jt-code" in services["prometheus"]["tmpfs"]
    for name in ("worker", "worker-documents"):
        assert services[name]["environment"]["CELERY_METRICS_PORT"] == "9808"


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example.com:4318"}, "OTEL_EXPORTER_OTLP_ENDPOINT"),
        ({"DJANGO_INTERNAL_HOSTS": "api,*"}, "DJANGO_INTERNAL_HOSTS"),
    ],
)
def test_monitoring_relaxations_stay_narrow(monkeypatch, override, expected):
    problems = _strict_problems(monkeypatch, {**_single_host_env(), **override})
    assert any(expected in problem for problem in problems), problems


def test_launchers_open_the_dashboards_for_both_stacks():
    launcher = (ROOT / "scripts/compose_up.sh").read_text()
    for url in ("http://127.0.0.1:3000/login", "http://127.0.0.1:9090/targets", "http://127.0.0.1:54323"):
        assert url in launcher
    assert "docker-compose.prod.yml" in launcher and "AUTO_OPEN_ONLY" in launcher
    tunnel = (ROOT / "scripts/prod_tunnel.sh").read_text()
    assert "ExitOnForwardFailure=yes" in tunnel and "AUTO_OPEN_ONLY=1" in tunnel
    makefile = (ROOT / "Makefile").read_text()
    assert "compose-up-prod:" in makefile and "prod-tunnel:" in makefile
