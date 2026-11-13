#!/usr/bin/env python3
"""Rebuild .env.docker from the environment of the running local API container.

The app containers load .env.docker (env_file) when they are created, so their environment holds every
value the file had, including the generated secrets the Supabase volumes were initialised with. Feed
`docker inspect` of the API container on stdin:

    sudo docker inspect jt-code-local-api-1 | .venv/bin/python scripts/restore_env_docker.py

The file is rebuilt in the layout of .env.docker.example; keys the example does not list are appended
under "Restored extra settings". SMTP_* is then taken from the repo .env (Gmail). Values are never printed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / ".env.docker"
# Set by the image or the runtime, not by .env.docker.
RUNTIME = re.compile(
    r"^(PATH|HOME|HOSTNAME|LANG|LC_.*|TERM|PYTHON.*|PIP_.*|GPG_KEY|VIRTUAL_ENV|UV_.*|DEBIAN_FRONTEND|TZ)$"
)


def compose_keys() -> set[str]:
    """Keys compose sets itself (environment:), which are derived values, not .env.docker entries."""
    service = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]["api"]
    env = service.get("environment", {})
    return set(env) if isinstance(env, dict) else {item.split("=", 1)[0] for item in env}


def main() -> int:
    if TARGET.exists() and TARGET.stat().st_size > 0 and "--force" not in sys.argv:
        print(f"{TARGET.name} is not empty; pass --force to overwrite it.", file=sys.stderr)
        return 1
    inspected = json.load(sys.stdin)
    container = inspected[0] if isinstance(inspected, list) else inspected
    values = dict(item.split("=", 1) for item in container["Config"]["Env"] if "=" in item)
    if "SUPABASE_DB_PASSWORD" not in values:
        print(
            "That container has no SUPABASE_DB_PASSWORD; inspect the API container (jt-code-local-api-1).",
            file=sys.stderr,
        )
        return 1

    smtp = {}
    for line in (ROOT / ".env").read_text().splitlines():
        match = re.match(r"^(SMTP_[A-Z_]+)=(.*)$", line)
        if match:
            smtp[match.group(1)] = match.group(2)

    out: list[str] = []
    used: set[str] = set()
    for line in (ROOT / ".env.docker.example").read_text().splitlines():
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if not match:
            out.append(line)
            continue
        key = match.group(1)
        used.add(key)
        if key in smtp:
            out.append(f"{key}={smtp[key]}")
        elif key in values:
            out.append(f"{key}={values[key]}")
        else:
            out.append(line)

    skip = used | compose_keys()
    extras = sorted(key for key in values if key not in skip and not RUNTIME.match(key))
    if extras:
        out += ["", "# --- Restored extra settings (were in .env.docker, not in the example) ---"]
        out += [f"{key}={values[key]}" for key in extras]

    TARGET.write_text("\n".join(out) + "\n")
    TARGET.chmod(0o600)
    restored = ", ".join(extras) or "none"
    print(f"Rebuilt {TARGET.name}: {len(used)} keys from the template, {len(extras)} extra: {restored}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
