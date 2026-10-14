#!/usr/bin/env python3
"""Snapshot the HTTP calls the frontend makes (method + path template).

    python scripts/contracts/extract_frontend_contract.py ../jt-code-frontend

Writes tests/fixtures/frontend_api_contract.json. ``tests/test_phase18_verification.py``
checks every entry against the backend's OpenAPI schema, so a backend change that
breaks the frontend fails CI even when the frontend repository is not checked out.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CALL = re.compile(r"\bapi\.(get|post|put|patch|delete)\s*(?:<[^()]*?>)?\s*\(\s*([`'\"])(.+?)\2", re.S)
# The repositories' ``list<T>(path)`` helper is a GET that unwraps plain arrays and DRF pages.
LIST = re.compile(r"\blist\s*<[^()]*?>\s*\(\s*([`'\"])(.+?)\1", re.S)
# ``blob(path)`` is a GET that downloads a file (exports).
BLOB = re.compile(r"\bblob\s*\(\s*([`'\"])(.+?)\1", re.S)
PARAM = re.compile(r"\$\{[^}]+\}")


def extract(frontend: Path) -> list[dict[str, str]]:
    calls: set[tuple[str, str]] = set()
    for source in sorted((frontend / "src" / "lib" / "data" / "api").glob("*.ts")):
        text = source.read_text()
        found = [(method, path) for method, _quote, path in CALL.findall(text)]
        found += [("get", path) for _quote, path in LIST.findall(text)]
        found += [("get", path) for _quote, path in BLOB.findall(text)]
        for method, path in found:
            template = PARAM.sub("{param}", path.split("?", 1)[0])
            if not template.startswith("/"):
                continue  # e.g. the helper's own `path` parameter
            calls.add((method.upper(), template))
    return [{"method": method, "path": path} for method, path in sorted(calls, key=lambda c: (c[1], c[0]))]


def main() -> int:
    frontend = Path(sys.argv[1] if len(sys.argv) > 1 else "../jt-code-frontend")
    contract = extract(frontend)
    target = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "frontend_api_contract.json"
    previous = json.loads(target.read_text()) if target.exists() else {}
    snapshot = {
        "source": "jt-code-frontend/src/lib/data/api",
        "calls": contract,
        # Reviewed annotations survive regeneration.
        "delegated": previous.get("delegated", {}),
        "deviations": previous.get("deviations", []),
    }
    target.write_text(json.dumps(snapshot, indent=2) + "\n")
    print(f"{len(contract)} calls -> {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
