"""Render the native (Docker-free) local stack: secrets, service configs and systemd user units.

    .venv/bin/python scripts/native/configure.py [--write-env]

* $JT_NATIVE_HOME/etc/secrets.env is generated once (database passwords, the
  Supabase JWT secret and anon/service_role keys, n8n's encryption key, the
  Kafka cluster id) and reused on every later run.
* infra/native/* templates are rendered into $JT_NATIVE_HOME/etc.
* One systemd *user* unit per process is written to ~/.config/systemd/user,
  each with a memory ceiling, grouped into targets:
      jt-code-infra.target          Kafka, Supabase Auth/REST/Storage/gateway, Mailpit, n8n
      jt-code-app.target            API, Celery worker + beat, Kafka consumer, Streamlit
      jt-code.target                both of the above
      jt-code-observability.target  Prometheus, Grafana, Tempo, OTel collector, exporters (opt-in)
* --write-env merges the connection settings for this stack into .env (a
  timestamped backup is kept) and drops the old Docker-only keys.

PostgreSQL 18 (+pgvector) and Redis are system services set up once by
scripts/native/bootstrap_system.sh.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "infra" / "native"
NATIVE_HOME = Path(os.environ.get("JT_NATIVE_HOME", Path.home() / ".local/share/jt-code-native"))
OPT, BIN, DATA, ETC = (NATIVE_HOME / name for name in ("opt", "bin", "data", "etc"))
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
VENV_BIN = REPO / ".venv" / "bin"
SYSTEM_NODE = "/usr/bin/node"  # n8n 1.88 supports Node <= 22; storage-api uses the private Node 24.

PORTS = {
    "GOTRUE_PORT": "9999",
    "POSTGREST_PORT": "54330",
    "POSTGREST_ADMIN_PORT": "54331",
    "STORAGE_PORT": "5000",
    "MAILPIT_SMTP_PORT": "1025",
}
SUPABASE_PUBLIC_URL = "http://localhost:54321"
WORKER_QUEUES = (
    "jobs.default,jobs.analysis,jobs.ingestion,jobs.visualization,"
    "analytics.analysis,analytics.visualization,orchestration"
)
# Settings this stack owns in .env. Values come from secrets.env / PORTS.
OBSOLETE_ENV_KEYS = (
    "SUPABASE_DB_URL",
    "STORAGE_PUBLIC_URL",
    "JT_CODE_LOCAL_SUPABASE_ANON_KEY",
    "JT_CODE_LOCAL_SUPABASE_SERVICE_ROLE_KEY",
    "JT_CODE_LOCAL_SUPABASE_PG_META_CRYPTO_KEY",
)


def _jwt(role: str, secret: str) -> str:
    sys.path.insert(0, str(REPO / "scripts"))
    from generate_supabase_keys import key  # noqa: PLC0415

    return key(role, secret)


def load_env_file(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values  # noqa: PLC0415

    return {k: v or "" for k, v in dotenv_values(path).items()} if path.exists() else {}


def ensure_secrets() -> dict[str, str]:
    path = ETC / "secrets.env"
    current = load_env_file(path)
    generated = {
        "JT_DB_PASSWORD": lambda: secrets.token_hex(24),
        "JT_ADMIN_DB_PASSWORD": lambda: secrets.token_hex(24),
        "JT_GRAFANA_ADMIN_PASSWORD": lambda: secrets.token_hex(16),
        "JT_SUPABASE_DB_PASSWORD": lambda: secrets.token_hex(24),
        "JT_N8N_DB_PASSWORD": lambda: secrets.token_hex(24),
        "JT_N8N_ENCRYPTION_KEY": lambda: secrets.token_hex(32),
        "JT_SUPABASE_JWT_SECRET": lambda: secrets.token_hex(32),
        "JT_SUPABASE_WEBHOOK_SIGNING_SECRET": lambda: secrets.token_hex(32),
        "JT_KAFKA_CLUSTER_ID": lambda: base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip("="),
    }
    changed = False
    for name, make in generated.items():
        if not current.get(name):
            current[name] = make()
            changed = True
    # The API keys are HS256 JWTs signed with the secret; regenerate only if missing.
    for role, name in (("anon", "JT_SUPABASE_ANON_KEY"), ("service_role", "JT_SUPABASE_SERVICE_ROLE_KEY")):
        if not current.get(name):
            current[name] = _jwt(role, current["JT_SUPABASE_JWT_SECRET"])
            changed = True
    if changed:
        write_private(path, "".join(f"{k}={v}\n" for k, v in current.items()))
        print(f"wrote {path}")
    return current


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    path.chmod(0o600)


def render_grafana_env(values: dict[str, str]) -> None:
    write_private(
        ETC / "grafana.env",
        f"GF_SECURITY_ADMIN_USER=admin\nGF_SECURITY_ADMIN_PASSWORD={values['JT_GRAFANA_ADMIN_PASSWORD']}\n",
    )


def render_templates(values: dict[str, str]) -> None:
    for name in (
        "kafka.properties",
        "Caddyfile",
        "gotrue.env",
        "postgrest.conf",
        "storage.env",
        "n8n.env",
        "tempo.yaml",
    ):
        text = Template((TEMPLATES / name).read_text()).substitute(values)
        write_private(ETC / name, text)
    print(f"rendered configs into {ETC}")


@dataclass
class Unit:
    name: str
    description: str
    exec_start: str
    target: str
    memory_max: str
    working_directory: Path = REPO
    environment: dict[str, str] = field(default_factory=dict)
    environment_file: Path | None = None
    exec_start_pre: str | None = None
    after: tuple[str, ...] = ()

    def render(self) -> str:
        unit = [
            "[Unit]",
            f"Description=JT-Code {self.description}",
            f"PartOf={self.target}",
        ]
        if self.after:
            unit.append(f"After={' '.join(self.after)}")
        service = [
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={self.working_directory}",
        ]
        if self.environment_file:
            service.append(f"EnvironmentFile={self.environment_file}")
        service.extend(f'Environment="{k}={v}"' for k, v in self.environment.items())
        if self.exec_start_pre:
            service.append(f"ExecStartPre={self.exec_start_pre}")
        service += [
            f"ExecStart={self.exec_start}",
            "Restart=on-failure",
            "RestartSec=3",
            # Hard ceiling per process so one runaway service cannot freeze the desktop.
            f"MemoryMax={self.memory_max}",
            "TimeoutStopSec=30",
            "",
        ]
        return "\n".join(unit + service)


def build_units(values: dict[str, str]) -> list[Unit]:
    python = VENV_BIN / "python"
    infra, app, obs = "jt-code-infra.target", "jt-code-app.target", "jt-code-observability.target"
    gateway_deps = (
        "jt-code-supabase-auth.service",
        "jt-code-supabase-rest.service",
        "jt-code-supabase-storage.service",
    )
    app_env = {"PYTHONUNBUFFERED": "1", "DJANGO_SETTINGS_MODULE": "config.settings.development"}
    return [
        # --- infrastructure -------------------------------------------------
        Unit(
            "jt-code-kafka",
            "Kafka broker (KRaft, single node)",
            f"{OPT}/kafka/bin/kafka-server-start.sh {ETC}/kafka.properties",
            infra,
            "768M",
            working_directory=DATA,
            environment={
                "JAVA_HOME": f"{OPT}/java",
                "KAFKA_HEAP_OPTS": "-Xms192m -Xmx384m",
                "LOG_DIR": f"{DATA}/kafka-logs",
            },
            exec_start_pre=(
                f"/bin/sh -c 'test -f {DATA}/kafka/meta.properties || "
                f"JAVA_HOME={OPT}/java {OPT}/kafka/bin/kafka-storage.sh format --standalone "
                f"-t {values['JT_KAFKA_CLUSTER_ID']} -c {ETC}/kafka.properties'"
            ),
        ),
        Unit(
            "jt-code-mailpit",
            "Mailpit (Supabase Auth email sink)",
            f"{BIN}/mailpit --listen 127.0.0.1:8025 --smtp 127.0.0.1:{PORTS['MAILPIT_SMTP_PORT']} "
            f"--database {DATA}/mailpit/mailpit.db",
            infra,
            "128M",
            working_directory=DATA,
        ),
        Unit(
            "jt-code-supabase-auth",
            "Supabase Auth (GoTrue)",
            f"{BIN}/gotrue",
            infra,
            "192M",
            working_directory=OPT / "gotrue",
            environment_file=ETC / "gotrue.env",
            after=("jt-code-mailpit.service",),
        ),
        Unit(
            "jt-code-supabase-rest",
            "Supabase Data API (PostgREST)",
            f"{BIN}/postgrest {ETC}/postgrest.conf",
            infra,
            "192M",
            working_directory=DATA,
        ),
        Unit(
            "jt-code-supabase-storage",
            "Supabase Storage API",
            f"{BIN}/node dist/start/server.js",
            infra,
            "512M",
            working_directory=OPT / "storage-api",
            environment_file=ETC / "storage.env",
            environment={"NODE_ENV": "production"},
            after=("jt-code-supabase-auth.service", "jt-code-supabase-rest.service"),
        ),
        Unit(
            "jt-code-supabase-gateway",
            "Supabase gateway (Caddy on :54321)",
            f"{BIN}/caddy run --config {ETC}/Caddyfile --adapter caddyfile",
            infra,
            "128M",
            working_directory=DATA,
            environment={"XDG_DATA_HOME": f"{DATA}/caddy", "XDG_CONFIG_HOME": f"{DATA}/caddy"},
            after=gateway_deps,
        ),
        Unit(
            "jt-code-n8n",
            "n8n workflow engine",
            f"{SYSTEM_NODE} {OPT}/n8n/node_modules/n8n/bin/n8n start",
            infra,
            "1G",
            working_directory=DATA / "n8n",
            environment_file=ETC / "n8n.env",
            environment={"NODE_OPTIONS": "--max-old-space-size=768"},
        ),
        # --- application ----------------------------------------------------
        Unit(
            "jt-code-api",
            "API (uvicorn, ASGI, auto-reload)",
            f"{VENV_BIN}/uvicorn config.asgi:application --env-file {REPO}/.env --host 127.0.0.1 --port 8000 "
            "--reload --reload-dir apps --reload-dir config",
            app,
            "1G",
            environment=app_env,
            after=("jt-code-infra.target",),
        ),
        Unit(
            "jt-code-worker",
            "Celery worker (every queue)",
            f"{VENV_BIN}/celery -A config worker --loglevel INFO --queues {WORKER_QUEUES} "
            "--concurrency 2 --max-tasks-per-child 200 --hostname worker@%%h",
            app,
            "2G",
            # PATH keeps /snap/bin for LibreOffice document conversion.
            environment={
                **app_env,
                "CELERY_METRICS_PORT": "9808",
                "PATH": f"{VENV_BIN}:/usr/bin:/bin:/snap/bin",
            },
            after=("jt-code-infra.target",),
        ),
        Unit(
            "jt-code-beat",
            "Celery beat scheduler",
            f"{VENV_BIN}/celery -A config beat --loglevel INFO --schedule {DATA}/celerybeat-schedule",
            app,
            "256M",
            environment=app_env,
            after=("jt-code-infra.target",),
        ),
        Unit(
            "jt-code-consumer",
            "Kafka consumer (integration webhooks)",
            f"{python} manage.py run_kafka_consumer integrations.webhook.received "
            "--consumer-name integration-webhooks --metrics-port 9809 "
            f"--heartbeat-file {DATA}/kafka-consumer.heartbeat",
            app,
            "512M",
            environment=app_env,
            after=("jt-code-infra.target",),
        ),
        Unit(
            "jt-code-streamlit",
            "Streamlit analytics app",
            f"{VENV_BIN}/streamlit run streamlit_app/app.py --server.address 127.0.0.1 --server.port 8501 "
            "--server.headless true --browser.gatherUsageStats false",
            app,
            "512M",
            environment={
                "JT_CODE_API_BASE_URL": "http://127.0.0.1:8000/api/v1",
                "SUPABASE_URL": SUPABASE_PUBLIC_URL,
                "SUPABASE_PUBLISHABLE_KEY": values["JT_SUPABASE_ANON_KEY"],
            },
            after=("jt-code-api.service",),
        ),
        # --- observability (opt-in) -----------------------------------------
        Unit(
            "jt-code-prometheus",
            "Prometheus",
            f"{BIN}/prometheus --config.file={REPO}/infra/prometheus/local.yml "
            f"--storage.tsdb.path={DATA}/prometheus --storage.tsdb.retention.time=3d "
            "--web.listen-address=127.0.0.1:9090",
            obs,
            "512M",
            working_directory=DATA,
        ),
        Unit(
            "jt-code-grafana",
            "Grafana",
            f"{OPT}/grafana/bin/grafana server --homepath {OPT}/grafana",
            obs,
            "384M",
            working_directory=OPT / "grafana",
            # GF_SECURITY_ADMIN_PASSWORD only seeds a new database; re-apply it on every start
            # so `make native-logins` always shows the working admin password.
            environment_file=ETC / "grafana.env",
            exec_start_pre=(
                '/bin/sh -c \'printf %%s "$GF_SECURITY_ADMIN_PASSWORD" | '
                f"{OPT}/grafana/bin/grafana cli --homepath {OPT}/grafana "
                f"--configOverrides cfg:default.paths.data={DATA}/grafana "
                "admin reset-admin-password --password-from-stdin >/dev/null'"
            ),
            environment={
                "GF_PATHS_DATA": f"{DATA}/grafana",
                "GF_PATHS_LOGS": f"{DATA}/grafana/log",
                "GF_PATHS_PROVISIONING": f"{TEMPLATES}/grafana/provisioning",
                "GF_SERVER_HTTP_ADDR": "127.0.0.1",
                "GF_SERVER_HTTP_PORT": "3000",
                "GF_AUTH_ANONYMOUS_ENABLED": "true",
                "GF_AUTH_ANONYMOUS_ORG_ROLE": "Viewer",
                "GF_USERS_VIEWERS_CAN_EDIT": "true",
                "GF_ANALYTICS_REPORTING_ENABLED": "false",
                "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
                "JT_CODE_GRAFANA_DASHBOARDS": f"{REPO}/infra/grafana/dashboards",
            },
        ),
        Unit(
            "jt-code-tempo",
            "Tempo (traces)",
            f"{BIN}/tempo -config.file={ETC}/tempo.yaml",
            obs,
            "384M",
            working_directory=DATA,
        ),
        Unit(
            "jt-code-otel-collector",
            "OpenTelemetry Collector",
            f"{BIN}/otelcol-contrib --config={REPO}/infra/otel/collector.yaml",
            obs,
            "256M",
            working_directory=DATA,
            environment={
                "OTEL_COLLECTOR_BIND": "127.0.0.1",
                "TRACES_BACKEND_ENDPOINT": "http://127.0.0.1:4319",
            },
            after=("jt-code-tempo.service",),
        ),
        Unit(
            "jt-code-redis-exporter",
            "Redis exporter",
            f"{BIN}/redis_exporter -redis.addr redis://127.0.0.1:6379 -web.listen-address 127.0.0.1:9121",
            obs,
            "64M",
            working_directory=DATA,
        ),
        Unit(
            "jt-code-kafka-console",
            "Kafka Console (Redpanda Console UI)",
            f"{BIN}/kafka-console",
            obs,
            "256M",
            working_directory=DATA,
            environment={
                "KAFKA_BROKERS": "127.0.0.1:9092",
                "SERVER_LISTENADDRESS": "127.0.0.1",
                "SERVER_LISTENPORT": "8080",
            },
        ),
    ]


def write_units(units: list[Unit]) -> None:
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    for stale in UNIT_DIR.glob("jt-code*"):
        stale.unlink()
    by_target: dict[str, list[str]] = {}
    for unit in units:
        (UNIT_DIR / f"{unit.name}.service").write_text(unit.render())
        by_target.setdefault(unit.target, []).append(f"{unit.name}.service")
    descriptions = {
        "jt-code-infra.target": "native infrastructure",
        "jt-code-app.target": "application processes",
        "jt-code-observability.target": "observability (opt-in)",
    }
    for target, members in by_target.items():
        extra = (
            "\nWants=jt-code-infra.target\nAfter=jt-code-infra.target"
            if target == "jt-code-app.target"
            else ""
        )
        (UNIT_DIR / target).write_text(
            f"[Unit]\nDescription=JT-Code {descriptions[target]}\nWants={' '.join(members)}{extra}\n"
        )
    (UNIT_DIR / "jt-code.target").write_text(
        "[Unit]\nDescription=JT-Code native local stack\nWants=jt-code-infra.target jt-code-app.target\n"
    )
    print(f"wrote {len(units)} units into {UNIT_DIR}")


def app_env_settings(values: dict[str, str]) -> dict[str, str]:
    db = f"postgresql://jt_code:{values['JT_DB_PASSWORD']}@127.0.0.1:5432"
    return {
        "DATABASE_URL": f"{db}/jt_code",
        "TEST_DATABASE_URL": f"{db}/jt_code_test",
        "DATABASE_POOLER_MODE": "direct",
        "REDIS_URL": "redis://localhost:6379/0",
        "CELERY_BROKER_URL": "redis://localhost:6379/1",
        "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
        "KAFKA_BOOTSTRAP_SERVERS": "localhost:9092",
        "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT",
        "KAFKA_TOPIC_REPLICATION_FACTOR": "1",
        "KAFKA_TOPIC_MIN_INSYNC_REPLICAS": "1",
        "SUPABASE_URL": SUPABASE_PUBLIC_URL,
        "SUPABASE_JWT_VERIFICATION": "hs256",
        "SUPABASE_JWT_SECRET": values["JT_SUPABASE_JWT_SECRET"],
        "SUPABASE_JWT_ISSUER": f"{SUPABASE_PUBLIC_URL}/auth/v1",
        "SUPABASE_JWT_AUDIENCE": "authenticated",
        "SUPABASE_SECRET_KEY": values["JT_SUPABASE_SERVICE_ROLE_KEY"],
        "SUPABASE_WEBHOOK_SIGNING_SECRET": values["JT_SUPABASE_WEBHOOK_SIGNING_SECRET"],
        "SUPABASE_STORAGE_BUCKET": "jt-code-assets",
        "N8N_BASE_URL": "http://localhost:5678",
        "N8N_WEBHOOK_BASE_URL": "http://localhost:5678",
        "N8N_CALLBACK_BASE_URL": "http://localhost:8000/api/v1",
    }


def write_env(values: dict[str, str]) -> None:
    path = REPO / ".env"
    wanted = app_env_settings(values)
    original = path.read_text() if path.exists() else ""
    lines = original.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in OBSOLETE_ENV_KEYS:
            continue
        if key in wanted:
            if key in seen:
                continue
            out.append(f"{key}={wanted[key]}")
            seen.add(key)
            continue
        out.append(line)
    missing = [k for k in wanted if k not in seen]
    if missing:
        out += ["", "# Native local stack (scripts/native/configure.py --write-env)"]
        out += [f"{k}={wanted[k]}" for k in missing]
    updated = "\n".join(out).rstrip("\n") + "\n"
    if updated == original:
        print(".env already up to date")
        return
    if original:
        backup = path.with_name(f".env.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"backed up .env to {backup.name}")
    write_private(path, updated)
    print(f"updated .env ({len(wanted)} native keys); reload .env in your editor before saving it")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--write-env", action="store_true", help="merge this stack's settings into .env")
    args = parser.parse_args(argv)
    for directory in (
        ETC,
        DATA / "kafka",
        DATA / "kafka-logs",
        DATA / "storage",
        DATA / "n8n",
        DATA / "mailpit",
        DATA / "prometheus",
        DATA / "grafana",
        DATA / "tempo",
        DATA / "caddy",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    values = ensure_secrets()
    repo_env = load_env_file(REPO / ".env")
    context = {
        **values,
        **PORTS,
        "SUPABASE_PUBLIC_URL": SUPABASE_PUBLIC_URL,
        "DATA_DIR": str(DATA),
        "OPT_DIR": str(OPT),
        "ASSET_MAX_UPLOAD_BYTES": repo_env.get("ASSET_MAX_UPLOAD_BYTES") or str(25 * 1024 * 1024),
        "N8N_DISPATCH_SECRET": repo_env.get("N8N_DISPATCH_SECRET", ""),
        "N8N_WEBHOOK_SECRET": repo_env.get("N8N_WEBHOOK_SECRET", ""),
        "N8N_SENTRY_RELAY_SECRET": repo_env.get("N8N_SENTRY_RELAY_SECRET", ""),
    }
    render_templates(context)
    render_grafana_env(values)
    write_units(build_units(values))
    if args.write_env:
        write_env(values)
    print("next: systemctl --user daemon-reload (make native-up does this)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
