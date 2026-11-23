#!/usr/bin/env python3
"""Live sweep of every operation in the API schema against the local Docker stack (``make docker-up``).

Two throwaway users (A and B, each in their own new workspace) are created confirmed, without mail
(see ``system_check.create_confirmed_user``). Then:

1. **Anonymous:** every operation without a token must not answer 2xx (except the public ones).
2. **Reads:** every GET as A. Collection responses provide real ids for the templated paths.
3. **Writes:** every POST/PUT/PATCH as A with an empty JSON body (validation must answer 4xx, never
   5xx), then every POST again with a minimal body built from its request schema, which creates
   records in many collections; DELETE only on templated paths. ``DELETE /settings/account/`` is
   never sent.
4. **Cross-tenant:** B reads, patches and deletes A's records by id; nothing may answer 2xx.

A 5xx fails the sweep, except a 503 whose code says a provider is not configured (an operator key).
Usage: scripts/endpoint_sweep.py [--report path.json]        (stdlib only, like system_check.py)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from system_check import API, ENV, ROOT, SUPABASE, create_confirmed_user, http  # noqa: E402

# Answer anonymous callers by design (health probes; world geometry for public shared dashboards; the
# plan catalogue on the pricing page; share/embed links are token-checked and use unknown tokens here).
PUBLIC = {
    ("GET", "/api/v1/health/live/"),
    ("GET", "/api/v1/health/ready/"),
    ("GET", "/api/v1/health/startup/"),
    ("GET", "/api/v1/visualize/maps/world/"),
}
NEVER_SEND = {("DELETE", "/api/v1/settings/account/")}
# A generated body would change the throwaway user's password and end the sweep's sign-in.
NO_GENERATED_BODY = {("POST", "/api/v1/accounts/me/password/")}
PARAM = re.compile(r"\{([^}]+)\}")


def schema() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "schema.json"
        subprocess.run(
            [
                sys.executable,
                "scripts/local_manage.py",
                "docker",
                "spectacular",
                "--api-version",
                "v1",
                "--format",
                "openapi-json",
                "--file",
                str(target),
            ],
            cwd=ROOT,
            capture_output=True,
            check=True,
            timeout=300,
        )
        return json.loads(target.read_text())


def sign_in(email: str, password: str) -> dict[str, str]:
    anon = ENV.get("SUPABASE_ANON_KEY", "")
    status, _, body = http(
        "POST",
        f"{SUPABASE}/auth/v1/token?grant_type=password",
        {"email": email, "password": password},
        {"apikey": anon, "Authorization": f"Bearer {anon}"},
    )
    if status != 200:
        raise SystemExit(f"sign-in failed for {email}: {status}")
    return {"Authorization": f"Bearer {body['access_token']}"}


def items(body: Any) -> list[dict[str, Any]]:
    """Records in a list response: a bare list, ``results``, or the first list-valued key."""
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if isinstance(body, dict):
        for key in ("results", "items", "data"):
            if isinstance(body.get(key), list):
                return [item for item in body[key] if isinstance(item, dict)]
        for value in body.values():
            if isinstance(value, list) and value and isinstance(value[0], dict) and "id" in value[0]:
                return value
    return []


def not_configured(body: Any) -> bool:
    text = json.dumps(body).lower() if not isinstance(body, str) else body.lower()
    return "not_configured" in text or "not configured" in text


def resolve(node: Any, spec: dict[str, Any]) -> dict[str, Any]:
    while isinstance(node, dict) and "$ref" in node:
        node = spec["components"]["schemas"][node["$ref"].rsplit("/", 1)[-1]]
    return node if isinstance(node, dict) else {}


def example(node: Any, spec: dict[str, Any], depth: int = 0) -> Any:
    """A minimal valid-looking value for a schema: required properties only, first enum choice."""
    node = resolve(node, spec)
    for key in ("allOf", "oneOf", "anyOf"):
        if node.get(key):
            return example(node[key][0], spec, depth)
    if "enum" in node:
        choices = [c for c in node["enum"] if c not in (None, "")]
        return choices[0] if choices else None
    kind, fmt = node.get("type"), node.get("format", "")
    if kind == "object" or "properties" in node:
        if depth > 3:
            return {}
        properties = node.get("properties", {})
        return {
            name: example(properties[name], spec, depth + 1)
            for name in node.get("required", [])
            if name in properties and not resolve(properties[name], spec).get("readOnly")
        }
    if kind == "array":
        return [example(node.get("items", {}), spec, depth + 1)] * max(1, node.get("minItems", 0) or 0)
    if kind == "integer":
        return max(1, node.get("minimum", 1) or 1)
    if kind == "number":
        return max(1.0, float(node.get("minimum", 1) or 1))
    if kind == "boolean":
        return False
    return {
        "uuid": str(uuid.uuid4()),
        "email": "sweep@example.com",
        "uri": "https://example.com/sweep",
        "date-time": "2026-01-01T00:00:00Z",
        "date": "2026-01-01",
    }.get(fmt, "sweep")


def request_body(spec: dict[str, Any], path: str, method: str) -> Any:
    content = spec["paths"][path][method.lower()].get("requestBody", {}).get("content", {})
    media = content.get("application/json")
    return example(media["schema"], spec) if media and "schema" in media else None


class Sweep:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        run = uuid.uuid4().hex[:8]
        self.users: dict[str, tuple[str, str]] = {}
        for name in ("a", "b"):
            email, password = f"sweep-{name}-{run}@jt-code.local", f"Sweep-{run}-{name.upper()}-Passw0rd!"
            create_confirmed_user(email, password, "Sweep", name.upper())
            self.users[name] = (email, password)
        self.auth = {name: sign_in(*creds) for name, creds in self.users.items()}
        self.ids: dict[str, list[str]] = {}  # collection path -> ids seen as A
        self.failures: list[str] = []
        self.needs_key: list[str] = []
        self.statuses: Counter[str] = Counter()
        self.slow: list[str] = []
        self.shared: set[str] = set()

    def call(self, who: str | None, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        headers = dict(self.auth[who]) if who else {}
        if method in {"POST", "PUT", "PATCH"}:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        for _ in range(6):  # throttles (300/min per IP, per-feature limits) answer 429: wait and retry
            started = time.time()
            status, response_headers, data = http(
                method, f"{API.removesuffix('/api/v1')}{path}", body, headers, timeout=60
            )
            elapsed = time.time() - started
            if status != 429:
                break
            wait = {k.lower(): v for k, v in response_headers.items()}.get("retry-after", "10")
            time.sleep(min(65.0, float(wait) if str(wait).replace(".", "", 1).isdigit() else 10.0))
        else:
            self.failures.append(f"{method} {path} still throttled after retries")
        if elapsed > 3:
            self.slow.append(f"{method} {path} {elapsed:.1f}s")
        return status, data

    def concrete(self, template: str, real: bool) -> str | None:
        """Fill path parameters: a real id of A's (from the parent collection) or a random UUID."""
        path = template
        for match in PARAM.finditer(template):
            prefix = template[: match.start()]
            known = self.ids.get(prefix) if real else None
            if real and not known:
                return None
            value = known[0] if known else str(uuid.uuid4())
            path = path.replace(match.group(0), value, 1)
        return path

    def record(self, label: str, status: int, data: Any) -> None:
        self.statuses[f"{status // 100}xx"] += 1
        if status >= 500:
            if status == 503 and not_configured(data):
                self.needs_key.append(f"{label} -> 503 {str(data)[:160]}")
            else:
                self.failures.append(f"{label} -> {status} {str(data)[:200]}")

    def still_signed_in(self, who: str, after: str) -> None:
        if self.call(who, "GET", "/api/v1/accounts/me/")[0] == 401:
            self.auth[who] = sign_in(*self.users[who])
            print(f"  note: {after} ended the session; signed in again")

    def shared_catalogue(self, template: str) -> bool:
        """True when B's own listing holds the record too: a platform catalogue (models, plans), not A's."""
        prefix = template[: PARAM.search(template).start()]
        known = self.ids.get(prefix) or []
        status, data = self.call("b", "GET", prefix)
        shared = status == 200 and bool(known) and known[0] in {str(item.get("id")) for item in items(data)}
        if shared:
            self.shared.add(prefix)
        return shared

    def seed(self) -> None:
        for path, body in (
            ("/api/v1/conversations/", {"title": "Sweep conversation"}),
            ("/api/v1/documents/", {"title": "Sweep document", "template": "general", "content": "v1"}),
            ("/api/v1/visualize/dashboards/", {"name": "Sweep board"}),
        ):
            status, data = self.call("a", "POST", path, body)
            if status in (200, 201) and isinstance(data, dict) and data.get("id"):
                self.ids.setdefault(path, []).insert(0, str(data["id"]))

    def run(self, operations: list[tuple[str, str]]) -> None:
        print(f"Endpoint sweep: {len(operations)} operations")
        for method, path in operations:
            if (method, path) in NEVER_SEND:
                continue
            status, data = self.call(
                None, method, self.concrete(path, real=False) or path, {} if method != "GET" else None
            )
            self.record(f"anonymous {method} {path}", status, data)
            if status < 300 and (method, path) not in PUBLIC:
                self.failures.append(f"anonymous {method} {path} answered {status}")
        print("  anonymous pass done")

        self.seed()
        for method, path in operations:
            if method == "GET" and not PARAM.search(path):
                status, data = self.call("a", "GET", path)
                self.record(f"A GET {path}", status, data)
                found = [str(item["id"]) for item in items(data) if item.get("id")]
                if found:
                    self.ids.setdefault(path, []).extend(i for i in found if i not in self.ids.get(path, []))
        for method, path in operations:
            if method == "GET" and PARAM.search(path):
                for real in (True, False):
                    concrete = self.concrete(path, real)
                    if concrete:
                        status, data = self.call("a", "GET", concrete)
                        self.record(f"A GET {path} ({'real' if real else 'unknown'} id)", status, data)
        print(f"  reads done ({sum(len(v) for v in self.ids.values())} ids from {len(self.ids)} collections)")

        for method, path in operations:
            if method in {"POST", "PUT", "PATCH"}:
                concrete = self.concrete(path, real=True) or self.concrete(path, real=False)
                status, data = self.call("a", method, concrete, {})
                self.record(f"A {method} {path} (empty body)", status, data)
                self.still_signed_in("a", f"{method} {path}")
        print("  empty-body writes done")

        created = 0
        for method, path in operations:
            if method != "POST" or (method, path) in NO_GENERATED_BODY:
                continue
            body = request_body(self.spec, path, method)
            if not body:
                continue
            concrete = self.concrete(path, real=True)
            if not concrete:
                continue
            status, data = self.call("a", method, concrete, body)
            self.record(f"A {method} {path} (generated body)", status, data)
            if status in (200, 201) and isinstance(data, dict) and data.get("id") and not PARAM.search(path):
                self.ids.setdefault(path, []).insert(0, str(data["id"]))
                created += 1
            self.still_signed_in("a", f"{method} {path}")
        print(f"  generated-body writes done ({created} records created)")

        # Cross-tenant: B must not reach A's records by id.
        probed = 0
        for method, path in operations:
            if not PARAM.search(path) or method not in {"GET", "PATCH", "PUT", "DELETE"}:
                continue
            concrete = self.concrete(path, real=True)
            if not concrete:
                continue
            probed += 1
            status, data = self.call("b", method, concrete, {} if method != "GET" else None)
            self.record(f"B {method} {path} (A's id)", status, data)
            if status < 300 and not self.shared_catalogue(path):
                self.failures.append(f"cross-tenant: B {method} {path} on A's record answered {status}")
        print(f"  cross-tenant pass done ({probed} probes)")

        for method, path in operations:
            if method == "DELETE" and PARAM.search(path):
                status, data = self.call("a", "DELETE", self.concrete(path, real=False) or path)
                self.record(f"A DELETE {path} (unknown id)", status, data)
        print("  deletes done")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, help="write the full result as JSON")
    args = parser.parse_args()

    spec = schema()
    operations = [
        (method.upper(), path)
        for path, entry in spec["paths"].items()
        for method in entry
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    sweep = Sweep(spec)
    started = time.time()
    sweep.run(operations)

    print(f"\nResponses by class: {dict(sorted(sweep.statuses.items()))} in {time.time() - started:.0f} s")
    print(f"Needs an operator key ({len(sweep.needs_key)}):")
    for line in sweep.needs_key:
        print(f"  ⚠ {line}")
    print(f"Slow (> 3 s): {len(sweep.slow)}")
    for line in sweep.slow[:20]:
        print(f"  · {line}")
    print(f"Shared catalogues (B lists the same records): {sorted(sweep.shared)}")
    print(f"Failures ({len(sweep.failures)}):")
    for line in sweep.failures:
        print(f"  ✗ {line}")
    if args.report:
        args.report.write_text(
            json.dumps(
                {
                    "operations": len(operations),
                    "statuses": sweep.statuses,
                    "needs_key": sweep.needs_key,
                    "slow": sweep.slow,
                    "shared_catalogues": sorted(sweep.shared),
                    "failures": sweep.failures,
                },
                indent=2,
            )
        )
    return 1 if sweep.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
