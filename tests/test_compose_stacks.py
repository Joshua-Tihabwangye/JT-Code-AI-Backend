"""The two Docker Compose stacks: production (compose.yaml + .env.production) and the local
full-Docker stack (compose.yaml + compose.local.yaml + .env.docker), and their env tooling."""

from __future__ import annotations

import importlib.util
import re
import shutil
import stat
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
APP_SERVICES = ("migrate", "api", "worker", "worker-documents", "beat", "consumer")


class _ComposeLoader(yaml.SafeLoader):
    """yaml.safe_load plus Compose's merge tags (!override, !reset)."""


for _tag in ("!override", "!reset"):
    _ComposeLoader.add_constructor(
        _tag,
        lambda loader, node: (
            loader.construct_sequence(node)
            if isinstance(node, yaml.SequenceNode)
            else loader.construct_mapping(node)
            if isinstance(node, yaml.MappingNode)
            else loader.construct_scalar(node)
        ),
    )


def _compose(name: str) -> dict:
    return yaml.load((ROOT / name).read_text(), Loader=_ComposeLoader)  # noqa: S506 - SafeLoader subclass


def _compose_env():
    spec = importlib.util.spec_from_file_location("compose_env", ROOT / "scripts" / "compose_env.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["compose_env"] = module  # dataclasses resolve their module while executing
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tool(tmp_path, monkeypatch):
    module = _compose_env()
    for name in (".env.docker.example", ".env.production.example", "compose.yaml", "compose.local.yaml"):
        shutil.copy(ROOT / name, tmp_path / name)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


# --- compose files --------------------------------------------------------------------------


def test_production_reads_only_env_production_never_the_developer_env():
    compose = _compose("compose.yaml")
    assert compose["x-app"]["env_file"] == [".env.production"]
    assert not re.search(r"env_file:\s*\[?\s*\.env\s*[\],\n]", (ROOT / "compose.yaml").read_text())


def test_local_override_swaps_env_file_and_profile_for_every_app_service():
    production = _compose("compose.yaml")["services"]
    local = _compose("compose.local.yaml")
    assert local["name"] == "jt-code-local"  # its volumes never mix with production's
    for name in APP_SERVICES:
        assert name in production
        service = local["services"][name]
        assert service["env_file"] == [".env.docker"], name
        assert service["environment"]["DJANGO_SETTINGS_MODULE"] == "config.settings.development"
        assert service["image"].endswith(":local") and service["image"] != production[name]["image"]
    override_text = (ROOT / "compose.local.yaml").read_text()
    assert override_text.count("env_file: !override [.env.docker]") == len(APP_SERVICES)


def test_local_stack_keeps_every_port_on_loopback_and_waits_for_mailpit():
    local = _compose("compose.local.yaml")["services"]
    for name, service in local.items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} beyond localhost"
    assert local["supabase-auth"]["depends_on"]["mailpit"] == {"condition": "service_healthy"}
    assert "healthcheck" in local["mailpit"]


def test_missing_compose_variables_name_the_launch_commands():
    text = (ROOT / "compose.yaml").read_text()
    messages = set(re.findall(r"\$\{\w+:\?([^}]*)\}", text))
    assert messages == {"use make docker-up (local) or make compose-up-prod (server)"}


# --- env templates --------------------------------------------------------------------------


@pytest.mark.parametrize("template", [".env.docker.example", ".env.production.example"])
def test_templates_list_every_secret_compose_requires(template):
    tool = _compose_env()
    keys = tool.parse_env((ROOT / template).read_text())
    required = set(re.findall(r"\$\{(\w+):\?", (ROOT / "compose.yaml").read_text()))
    assert required <= set(keys), required - set(keys)
    for key in tool.GENERATED:
        assert key in keys and keys[key] == "", f"{template} must leave {key} empty for the generator"
    for key in tool.SUPABASE_KEYS:
        assert keys.get(key) == ""


def test_secret_files_are_ignored_and_kept_out_of_the_image():
    gitignore = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env.*" in gitignore and "!.env.docker.example" in gitignore
    assert "!.env.docker" not in gitignore and "!.env.production" not in gitignore
    dockerignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert ".env.*" in dockerignore and "!.env.docker.example" not in dockerignore


# --- scripts/compose_env.py -----------------------------------------------------------------


def test_init_generates_every_secret_and_valid_supabase_keys(tool, tmp_path):
    assert tool.init(tool.STACKS["local"]) == 0
    target = tmp_path / ".env.docker"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    env = tool.parse_env(target.read_text())
    for key in tool.GENERATED:
        assert env[key], key
    assert len(env["DJANGO_SECRET_KEY"]) >= 50
    assert len({env[k] for k in tool.GENERATED}) == len(tool.GENERATED)  # no reused secret
    for key, role in tool.SUPABASE_KEYS.items():
        assert tool.key_problem(env[key], role, env["SUPABASE_JWT_SECRET"]) is None


def test_init_never_rotates_existing_secrets_and_appends_new_template_keys(tool, tmp_path):
    stack = tool.STACKS["production"]
    tool.init(stack)
    target = tmp_path / ".env.production"
    first = tool.parse_env(target.read_text())
    (tmp_path / ".env.production.example").write_text(
        (tmp_path / ".env.production.example").read_text() + "\nBRAND_NEW_SETTING=42\n"
    )
    tool.init(stack)
    second = tool.parse_env(target.read_text())
    assert {k: second[k] for k in first} == first
    assert second["BRAND_NEW_SETTING"] == "42"


def test_init_re_signs_supabase_keys_after_the_jwt_secret_changes(tool, tmp_path):
    stack = tool.STACKS["local"]
    tool.init(stack)
    target = tmp_path / ".env.docker"
    text = target.read_text()
    old = tool.parse_env(text)
    target.write_text(text.replace(old["SUPABASE_JWT_SECRET"], "f" * 64))
    tool.init(stack)
    env = tool.parse_env(target.read_text())
    assert env["SUPABASE_ANON_KEY"] != old["SUPABASE_ANON_KEY"]
    assert tool.key_problem(env["SUPABASE_ANON_KEY"], "anon", "f" * 64) is None


def test_local_init_copies_integrations_but_never_connection_settings_from_dev_env(tool, tmp_path):
    dev = tmp_path / ".env"
    dev.write_text(
        "GEMINI_API_KEY=gemini-test-key\n"  # pragma: allowlist secret
        "DATABASE_URL=postgresql://dev@localhost/postgres\n"
        "REDIS_URL=redis://localhost:6379/0\n"
        "DJANGO_SETTINGS_MODULE=config.settings.development\n"
        "SUPABASE_DB_PASSWORD=dev-password\n"  # pragma: allowlist secret
    )
    tool.init(tool.STACKS["local"], dev_env=dev)
    env = tool.parse_env((tmp_path / ".env.docker").read_text())
    assert env["GEMINI_API_KEY"] == "gemini-test-key"  # pragma: allowlist secret
    assert "DATABASE_URL" not in env and "REDIS_URL" not in env and "DJANGO_SETTINGS_MODULE" not in env
    assert env["SUPABASE_DB_PASSWORD"] != "dev-password"  # pragma: allowlist secret
    assert dev.read_text().count("\n") == 5  # the developer's file is never written


def test_key_problem_rejects_foreign_or_wrong_role_keys():
    tool = _compose_env()
    secret = "a" * 64
    anon = tool.supabase_key("anon", secret)
    assert tool.key_problem(anon, "anon", secret) is None
    assert "SUPABASE_JWT_SECRET" in tool.key_problem(anon, "anon", "b" * 64)
    assert "role" in tool.key_problem(anon, "service_role", secret)
    assert tool.key_problem("not-a-jwt", "anon", secret) == "is not a JWT"


def test_check_reports_missing_file_and_template_placeholders(tool, tmp_path, capsys):
    stack = tool.STACKS["production"]
    assert tool.check(stack) == 1
    assert "make prod-env" in capsys.readouterr().err
    tool.init(stack)
    assert tool.check(stack) == 1  # domains are still example.com; validation would fail later
    err = capsys.readouterr().err
    assert "SUPABASE_PUBLIC_URL still holds the template value" in err


# --- launcher -------------------------------------------------------------------------------


def test_launcher_runs_the_preflight_before_up_and_never_invents_production_secrets():
    launcher = (ROOT / "scripts" / "compose_up.sh").read_text()
    main = launcher.split("main() {", 1)[1]
    assert main.index("compose_env.py check") < main.index(" up --build")
    prepare = launcher.split("prepare_env() {", 1)[1].split("\n}\n", 1)[0]
    assert prepare.index('"$STACK" != local') < prepare.index("compose_env.py init local")
    assert "volume inspect" in prepare  # a fresh env file would orphan existing volumes
    wrapper = (ROOT / "scripts" / "compose.sh").read_text()
    assert "-f compose.yaml -f compose.local.yaml --env-file .env.docker" in wrapper
    assert "-f compose.yaml --env-file .env.production" in wrapper
    makefile = (ROOT / "Makefile").read_text()
    for target in ("docker-up:", "docker-down:", "docker-reset:", "prod-env:", "prod-check:"):
        assert target in makefile


# --- health and database access ---------------------------------------------------------------

ONE_SHOT = {"kafka-setup", "migrate"}


def test_every_long_running_service_has_a_health_check():
    services = {**_compose("compose.yaml")["services"]}
    for name, override in _compose("compose.local.yaml")["services"].items():
        services[name] = {**services.get(name, {}), **override}
    missing = [n for n, svc in services.items() if n not in ONE_SHOT and "healthcheck" not in svc]
    assert not missing, f"no healthcheck: {missing}"


def test_prometheus_token_tmpfs_is_owned_by_the_prometheus_user():
    prometheus = _compose("compose.yaml")["services"]["prometheus"]
    (mount,) = prometheus["tmpfs"]
    assert mount.startswith("/run/jt-code:") and "uid=65534" in mount and "gid=65534" in mount
    assert "/run/jt-code/metrics-token" in prometheus["command"][0]


def test_monitoring_never_gates_the_application_start():
    services = _compose("compose.yaml")["services"]
    assert services["grafana"]["depends_on"]["prometheus"]["condition"] == "service_started"
    for name in APP_SERVICES:
        assert not {"prometheus", "grafana"} & set(services[name].get("depends_on", {}))


def test_database_is_reachable_for_pgadmin_on_loopback_and_studio_is_gone():
    services = _compose("compose.yaml")["services"]
    assert services["supabase-db"]["ports"] == ["127.0.0.1:54322:5432"]
    assert "supabase-studio" not in services and "supabase-meta" not in services
    assert "PG_META" not in (ROOT / "compose.yaml").read_text()
    assert "54322" in (ROOT / "scripts" / "prod_tunnel.sh").read_text()
