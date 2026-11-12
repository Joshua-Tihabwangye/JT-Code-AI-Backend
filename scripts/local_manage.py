#!/usr/bin/env python3
"""Run host ``manage.py`` against the database of the local Docker stack.

The host ``.env`` belongs to the native stack (Postgres on :5432). The Docker stack publishes its Supabase
Postgres on 127.0.0.1:54322 and keeps its password in ``.env.docker``. This wrapper reads that password
in-process (it is never printed) and runs ``manage.py`` with ``DATABASE_URL`` pointed there, so tests and
operators can use DEBUG-only commands such as ``grant_local_credits`` without sudo or container access.

Usage: scripts/local_manage.py docker <manage.py arguments>
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STACKS = {"docker": (".env.docker", "127.0.0.1", 54322, "postgres", "postgres")}


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in STACKS:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    env_file, host, port, user, database = STACKS[argv[1]]
    password = read_env(ROOT / env_file).get("SUPABASE_DB_PASSWORD")
    if not password:
        print(f"SUPABASE_DB_PASSWORD missing in {env_file}; run make docker-env first.", file=sys.stderr)
        return 1
    from urllib.parse import quote

    env = dict(os.environ)
    env.update(
        DATABASE_URL=f"postgresql://{user}:{quote(password, safe='')}@{host}:{port}/{database}",
        DJANGO_SETTINGS_MODULE="config.settings.development",
        DJANGO_DEBUG="true",
    )
    python = ROOT / ".venv/bin/python"
    return subprocess.call(
        [str(python if python.exists() else sys.executable), "manage.py", *argv[2:]], cwd=ROOT, env=env
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
