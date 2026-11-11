#!/usr/bin/env python3
"""Create and check the env files of the two Docker Compose stacks.

    python3 scripts/compose_env.py init  local        # .env.docker     (compose.yaml + compose.local.yaml)
    python3 scripts/compose_env.py init  production   # .env.production (compose.yaml)
    python3 scripts/compose_env.py check local|production

``init`` copies the stack's template (.env.docker.example / .env.production.example), fills every
empty generated secret and derives the Supabase API keys from SUPABASE_JWT_SECRET. It never
replaces a value that is already set: the database volume keeps the passwords it was initialised
with, so a rotated password would lock the services out. Template keys missing from an existing
file are appended. The file is written with mode 600.

``check`` is the preflight that scripts/compose_up.sh runs before ``docker compose up``: every
``${VAR:?}`` the compose files require is set, the Supabase keys match the JWT secret, template
placeholders are gone, and each app container's resolved environment (``docker compose config``)
passes the settings validation of its profile (config/settings/validation.py).

Standard library only, so it runs on a fresh server before any Python dependency is installed.
The developer's native ``.env`` is never written.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEN_YEARS = 10 * 365 * 24 * 3600
APP_SERVICES = ("migrate", "api", "worker", "worker-documents", "beat", "consumer")


@dataclass(frozen=True)
class Stack:
    name: str
    env_file: str
    template: str
    compose_files: tuple[str, ...]


STACKS = {
    "local": Stack("local", ".env.docker", ".env.docker.example", ("compose.yaml", "compose.local.yaml")),
    "production": Stack("production", ".env.production", ".env.production.example", ("compose.yaml",)),
}


def _hex() -> str:
    return secrets.token_hex(32)  # hex keeps redis:// and postgresql:// URLs valid without escaping


def _fernet() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


# Secrets this tool may generate. Everything else (domains, SMTP, Stripe, Sentry, AI keys) is the
# operator's to supply and is reported by ``check`` when a profile requires it.
GENERATED = {
    "SUPABASE_DB_PASSWORD": _hex,
    "SUPABASE_JWT_SECRET": _hex,
    "SUPABASE_WEBHOOK_SIGNING_SECRET": _hex,
    "REDIS_CACHE_PASSWORD": _hex,
    "REDIS_BROKER_PASSWORD": _hex,
    "KAFKA_ADMIN_PASSWORD": _hex,
    "KAFKA_PRODUCER_PASSWORD": _hex,
    "KAFKA_CONSUMER_PASSWORD": _hex,
    "GRAFANA_ADMIN_PASSWORD": lambda: secrets.token_hex(16),
    "DJANGO_SECRET_KEY": lambda: secrets.token_urlsafe(64),
    "METRICS_AUTH_TOKEN": _hex,
    "WEBHOOK_SIGNING_SECRET": _hex,
    "N8N_DISPATCH_SECRET": _hex,
    "N8N_WEBHOOK_SECRET": _hex,
    "N8N_SENTRY_RELAY_SECRET": _hex,
    "TOOL_CREDENTIALS_ENCRYPTION_KEYS": _fernet,
}
SUPABASE_KEYS = {"SUPABASE_ANON_KEY": "anon", "SUPABASE_SERVICE_ROLE_KEY": "service_role"}

# Local stack only: integrations and tunables copied from the developer's native .env when the
# local file does not set them. Connection settings are never copied (compose.yaml owns them).
COPY_FROM_DEV_PREFIXES = (
    "AI_",
    "AGENT_",
    "GEMINI_",
    "OPENAI_",
    "LLAMA_",
    "RAG_",
    "VECTOR_",
    "STRIPE_",
    "SEARCH_API_",
    "GITHUB_",
    "BILLING_",
    "THROTTLE_",
    "CHAT_",
    "MCP_",
)
COPY_FROM_DEV_KEYS = ("SENTRY_DSN", "PGVECTOR_ENABLED", "BROWSER_TOOL_ENABLED", "ENABLE_MCP")

_LINE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")
_PLACEHOLDER_HOSTS = ("example.com", "example.org", "example.net")


# --- env files -----------------------------------------------------------------------------


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if match := _LINE.match(line.strip()):
            value = match.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[match.group(1)] = value
    return values


def _set_line(lines: list[str], key: str, value: str) -> None:
    for index, line in enumerate(lines):
        if (match := _LINE.match(line.strip())) and match.group(1) == key:
            lines[index] = f"{key}={value}"
            return
    lines.append(f"{key}={value}")


# --- Supabase API keys (HS256 JWTs, as GoTrue/PostgREST/Storage expect) ----------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def supabase_key(role: str, secret: str) -> str:
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(
        json.dumps(
            {"role": role, "iss": "supabase", "iat": now, "exp": now + TEN_YEARS}, separators=(",", ":")
        ).encode()
    )
    signature = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


def key_problem(token: str, role: str, secret: str) -> str | None:
    """Why ``token`` is not a valid, unexpired ``role`` key for ``secret`` (None when it is)."""
    try:
        header, payload, signature = token.split(".")
        expected = _b64(hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError, json.JSONDecodeError:
        return "is not a JWT"
    if not hmac.compare_digest(signature, expected):
        return "was not signed with SUPABASE_JWT_SECRET"
    if claims.get("role") != role:
        return f"has role {claims.get('role')!r}, expected {role!r}"
    if int(claims.get("exp", 0)) < time.time() + 30 * 24 * 3600:
        return "expires within 30 days"
    return None


# --- init ----------------------------------------------------------------------------------


def init(stack: Stack, *, dev_env: Path | None = None) -> int:
    target, template = ROOT / stack.env_file, ROOT / stack.template
    created = not target.exists()
    lines = (template if created else target).read_text().splitlines()
    present = parse_env("\n".join(lines))
    changed: list[str] = []

    if not created:  # keep an existing file in step with a template that gained keys
        missing = [
            line
            for line in template.read_text().splitlines()
            if (m := _LINE.match(line)) and m.group(1) not in present
        ]
        if missing:
            lines += ["", f"# --- Added from {stack.template} by scripts/compose_env.py ---", *missing]
            changed += [_LINE.match(line).group(1) for line in missing]
            present = parse_env("\n".join(lines))

    if stack.name == "local" and dev_env and dev_env.exists():
        copied = [
            (key, value)
            for key, value in parse_env(dev_env.read_text()).items()
            if value
            and not present.get(key)
            and (key.startswith(COPY_FROM_DEV_PREFIXES) or key in COPY_FROM_DEV_KEYS)
            and key not in GENERATED
        ]
        if copied:
            lines += ["", f"# --- Copied from {dev_env.name} (integrations and tunables) ---"]
            for key, value in copied:
                _set_line(lines, key, value)
            changed += [key for key, _ in copied]
            present = parse_env("\n".join(lines))

    for key, generate in GENERATED.items():
        if key in present and not present[key]:
            _set_line(lines, key, generate())
            changed.append(key)
    present = parse_env("\n".join(lines))

    secret = present.get("SUPABASE_JWT_SECRET", "")
    for key, role in SUPABASE_KEYS.items():
        if key in present and secret and key_problem(present[key], role, secret):
            _set_line(lines, key, supabase_key(role, secret))
            changed.append(key)

    if created or changed:
        target.write_text("\n".join(lines).rstrip() + "\n")
    target.chmod(0o600)

    verb = "Created" if created else ("Updated" if changed else "Unchanged:")
    print(f"{verb} {stack.env_file}" + (f" ({len(changed)} values: {', '.join(changed)})" if changed else ""))
    if stack.name == "production":
        print(
            "Next: fill the values only you can supply (domains, SMTP, Stripe, Sentry, Gemini, n8n),\n"
            "      then run: python3 scripts/compose_env.py check production"
        )
    return 0


# --- check ---------------------------------------------------------------------------------


def _load_validation():
    path = ROOT / "config" / "settings" / "validation.py"
    spec = importlib.util.spec_from_file_location("jt_code_settings_validation", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)  # stdlib only; Django is imported lazily and never here
    return module


def _validate(environment: dict[str, str], profile: str) -> list[str]:
    validation = _load_validation()
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update({k: str(v) for k, v in environment.items() if v is not None})
        return list(validation.validate_environment(profile))
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _compose_config(stack: Stack) -> dict:
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("docker is not installed")
    command = [docker, "compose"]
    for name in stack.compose_files:
        command += ["-f", name]
    command += ["--env-file", stack.env_file, "config", "--format", "json"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "docker compose config failed")
    return json.loads(result.stdout)


def check(stack: Stack) -> int:
    target = ROOT / stack.env_file
    if not target.exists():
        hint = "make docker-env" if stack.name == "local" else "make prod-env"
        print(f"✗ {stack.env_file} does not exist: run `{hint}` first.", file=sys.stderr)
        return 1
    if target.stat().st_mode & 0o077:
        target.chmod(0o600)
        print(f"! {stack.env_file} was readable by other users; set to mode 600.")

    env = parse_env(target.read_text())
    problems: list[str] = []

    required = set()
    for name in stack.compose_files:
        required |= set(re.findall(r"\$\{(\w+):\?", (ROOT / name).read_text()))
    problems += [f"{key} is empty in {stack.env_file}" for key in sorted(required) if not env.get(key)]

    secret = env.get("SUPABASE_JWT_SECRET", "")
    for key, role in SUPABASE_KEYS.items():
        if secret and env.get(key) and (why := key_problem(env[key], role, secret)):
            problems.append(f"{key} {why}: run `python3 scripts/compose_env.py init {stack.name}`")

    if stack.name == "production":
        problems += [
            f"{key} still holds the template value {value!r}"
            for key, value in env.items()
            if any(host in value for host in _PLACEHOLDER_HOSTS)
        ]

    if not problems:  # interpolation would fail anyway; report the file first
        try:
            config = _compose_config(stack)
        except RuntimeError as exc:
            problems.append(f"docker compose config: {exc}")
        else:
            seen: set[str] = set()
            for service in APP_SERVICES:
                environment = config["services"][service].get("environment") or {}
                profile = environment.get("DJANGO_SETTINGS_MODULE", "").rsplit(".", 1)[-1]
                for problem in _validate(environment, profile):
                    if problem not in seen:
                        seen.add(problem)
                        problems.append(f"[{profile}] {problem}")

    if problems:
        print(f"✗ {stack.env_file} is not ready for the {stack.name} stack:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"✓ {stack.env_file} passes the {stack.name} preflight ({len(env)} values).")
    return 0


def frontend(stack: Stack) -> int:
    env = parse_env((ROOT / stack.env_file).read_text())
    print(f"VITE_SUPABASE_URL={env.get('SUPABASE_PUBLIC_URL', '')}")
    print(f"VITE_SUPABASE_ANON_KEY={env.get('SUPABASE_ANON_KEY', '')}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"init", "check", "frontend"} or argv[2] not in STACKS:
        print(__doc__.strip(), file=sys.stderr)
        return 64
    stack = STACKS[argv[2]]
    if argv[1] == "init":
        return init(stack, dev_env=ROOT / ".env")
    if argv[1] == "frontend":
        return frontend(stack)
    return check(stack)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
