#!/usr/bin/env python3
"""Validate the compose files without real secrets: ``docker compose config -q`` for every stack.

    python3 scripts/compose_config_check.py

The ``.env*.example`` templates leave generated secrets empty, and compose refuses empty ``${VAR:?}``
values, and ``env_file:`` needs the real file names (``.env.production``/``.env.docker``). This copies the
compose files to a temporary directory, writes each template there under its real name with every empty
value filled by a dummy, and runs ``docker compose ... config -q`` (production, production + the edge
profile, local). It needs only the docker CLI, not the daemon, so it works without sudo. Nothing in the
repository is written.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STACKS = {
    "production": (".env.production.example", ["compose.yaml"], []),
    "production+edge": (".env.production.example", ["compose.yaml"], ["edge"]),
    "local": (".env.docker.example", ["compose.yaml", "compose.local.yaml"], []),
}


def _filled(template: Path) -> str:
    lines = []
    for line in template.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#") and not value.strip():
            line = f"{key}=placeholder-{key.lower()}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        for compose_file in ROOT.glob("compose*.yaml"):
            shutil.copy(compose_file, work / compose_file.name)
        (work / ".env.production").write_text(_filled(ROOT / ".env.production.example"))
        (work / ".env.docker").write_text(_filled(ROOT / ".env.docker.example"))
        for name, (template, files, profiles) in STACKS.items():
            env_file = work / (".env.docker" if template.startswith(".env.docker") else ".env.production")
            command = ["docker", "compose", "--project-directory", str(work), "--env-file", str(env_file)]
            for file in files:
                command += ["-f", str(work / file)]
            for profile in profiles:
                command += ["--profile", profile]
            result = subprocess.run([*command, "config", "-q"], capture_output=True, text=True, check=False)
            ok = result.returncode == 0
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {name}")
            if not ok:
                print(result.stderr.strip(), file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
