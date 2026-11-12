#!/usr/bin/env python3
"""Whole-system check of the local Docker stack (``make docker-up``), end to end.

Signs up a fresh user through Supabase Auth (confirming the address through Mailpit), drives the API the
way the frontend does, then verifies the effects where they land: rows in Postgres, Celery tasks, Kafka
(outbox -> Redpanda -> consumer), Redis, Prometheus metrics, Grafana, tracing and Storage. Prints one
pass/fail line per check and exits non-zero when any check fails.

Usage: scripts/system_check.py            (stdlib only; DB queries go through scripts/local_manage.py)
Secrets (anon key, Grafana password) are read from .env.docker in-process and never printed.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:8000/api/v1"
SUPABASE = "http://127.0.0.1:54321"
MAILPIT = "http://127.0.0.1:8025"
PROMETHEUS = "http://127.0.0.1:9090"
GRAFANA = "http://127.0.0.1:3000"
CONSOLE = "http://127.0.0.1:8080"
FRONTEND_ORIGIN = "http://localhost:5173"

results: list[tuple[str, str, bool, str]] = []


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


ENV = read_env(ROOT / ".env.docker")


def http(
    method: str, url: str, body: Any = None, headers: dict[str, str] | None = None, timeout: float = 60
) -> tuple[int, dict[str, str], Any]:
    """Returns (status, headers, JSON or text); never raises on HTTP errors, never follows redirects."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})}
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status, response_headers, raw = response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        status, response_headers, raw = error.code, dict(error.headers), error.read()
    text = raw.decode(errors="replace")
    try:
        return status, response_headers, json.loads(text)
    except ValueError:
        return status, response_headers, text


def check(section: str, name: str, fn: Callable[[], str | None]) -> Any:
    try:
        detail = fn() or ""
        results.append((section, name, True, detail))
        print(f"  ✓ {section:<11} {name}{f'  ({detail})' if detail else ''}", flush=True)
    except Exception as error:  # noqa: BLE001 - every failure is reported, not raised
        results.append((section, name, False, str(error)))
        print(f"  ✗ {section:<11} {name}: {error}", flush=True)


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def wait_for(
    load: Callable[[], Any], done: Callable[[Any], bool], timeout: float = 90, what: str = "condition"
) -> Any:
    deadline = time.time() + timeout
    while True:
        value = load()
        if done(value):
            return value
        if time.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}; last: {str(value)[:200]}")
        time.sleep(1.5)


def prom(query: str) -> float:
    status, _, body = http("GET", f"{PROMETHEUS}/api/v1/query?{urllib.parse.urlencode({'query': query})}")
    expect(status == 200, f"Prometheus query failed: {status}")
    values = [float(item["value"][1]) for item in body["data"]["result"]]
    return sum(values) if values else 0.0


def db(code: str) -> Any:
    """Runs Python in Django against the Docker database; the code prints one JSON line."""
    output = subprocess.run(
        [sys.executable, "scripts/local_manage.py", "docker", "shell", "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    lines = [line for line in output.stdout.splitlines() if line.startswith("{")]
    expect(bool(lines), f"DB query produced no result: {output.stderr.strip()[-300:]}")
    return json.loads(lines[-1])


def main() -> int:
    run = uuid.uuid4().hex[:8]
    email, password = f"system-{run}@jt-code.local", f"System-{run}-Passw0rd!"
    anon = ENV.get("SUPABASE_ANON_KEY", "")
    state: dict[str, Any] = {}
    started = time.time()
    print(f"JT-Code system check ({email})")

    # Baselines for counters, compared at the end.
    base = {
        "http": prom("sum(jt_http_requests_total)"),
        "celery_ok": prom('sum(jt_celery_tasks_total{state="succeeded"})'),
        "celery_failed": prom('sum(jt_celery_tasks_total{state="failed"})'),
        "outbox": prom("sum(jt_outbox_published_total)"),
        "offsets": prom("sum(redpanda_kafka_max_offset)"),
        "redis_cmds": prom("sum(redis_commands_processed_total)"),
    }

    # --- API ---------------------------------------------------------------------------------------
    check("api", "live probe", lambda: expect(http("GET", f"{API}/health/live/")[0] == 200, "not live"))

    def ready() -> str:
        status, _, body = http("GET", f"{API}/health/ready/")
        expect(status == 200 and all(v == "ok" for v in body["checks"].values()), f"{status} {body}")
        return ", ".join(f"{k}={v}" for k, v in body["checks"].items())

    check("api", "readiness (db, redis, broker, kafka)", ready)

    # --- CORS (the browser at :5173 must reach both Supabase and the API) ----------------------------
    def cors(url: str, headers: str) -> str:
        status, response_headers, _ = http(
            "OPTIONS",
            url,
            headers={
                "Origin": FRONTEND_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": headers,
            },
        )
        allowed = {k.lower(): v for k, v in response_headers.items()}.get("access-control-allow-origin")
        expect(
            status < 400 and allowed in {"*", FRONTEND_ORIGIN}, f"preflight {status}, allow-origin={allowed}"
        )
        return f"allow-origin {allowed}"

    check(
        "cors",
        "Supabase Auth from the app",
        lambda: cors(f"{SUPABASE}/auth/v1/signup", "apikey,authorization,content-type,x-client-info"),
    )
    check(
        "cors",
        "API from the app",
        lambda: cors(f"{API}/conversations/", "authorization,content-type,idempotency-key"),
    )

    # --- Auth ----------------------------------------------------------------------------------------
    supa_headers = {"apikey": anon, "Authorization": f"Bearer {anon}"}

    def sign_up() -> str:
        status, _, body = http(
            "POST",
            f"{SUPABASE}/auth/v1/signup",
            {"email": email, "password": password, "data": {"first_name": "System", "last_name": "Check"}},
            supa_headers,
        )
        expect(status == 200, f"sign-up {status}: {body}")
        return "confirmation required" if not body.get("access_token") else "auto-confirmed"

    check("auth", "sign up (Supabase GoTrue)", sign_up)

    def confirm() -> str:
        def link() -> str:
            messages = http("GET", f"{MAILPIT}/api/v1/messages")[2]["messages"]
            message = next(
                (
                    m
                    for m in messages
                    if re.search("confirm", m["Subject"], re.I)
                    and any(t["Address"] == email for t in m["To"])
                ),
                None,
            )
            if not message:
                return ""
            html = http("GET", f"{MAILPIT}/api/v1/message/{message['ID']}")[2]["HTML"]
            found = re.search(r'href="([^"]*/verify[^"]*)"', html)
            return found.group(1).replace("&amp;", "&") if found else ""

        url = wait_for(link, bool, 30, "the confirmation mail in Mailpit")
        status = http("GET", url)[0]
        expect(status in (200, 302, 303), f"verify link answered {status}")
        return "mail received in Mailpit, link verified"

    check("auth", "email confirmation (Mailpit)", confirm)

    def sign_in() -> str:
        status, _, body = http(
            "POST",
            f"{SUPABASE}/auth/v1/token?grant_type=password",
            {"email": email, "password": password},
            supa_headers,
        )
        expect(status == 200 and body.get("access_token"), f"sign-in {status}: {body}")
        state["token"] = body["access_token"]
        return "access token issued"

    check("auth", "sign in with password", sign_in)
    token = state.get("token")
    if not token:
        return report(started)
    auth = {"Authorization": f"Bearer {token}"}

    def me() -> str:
        status, _, body = http("GET", f"{API}/accounts/me/", headers=auth)
        expect(status == 200 and body.get("email") == email, f"{status} {body}")
        return f"plan {body.get('plan')}"

    check("auth", "Django accepts the Supabase session", me)
    check(
        "auth",
        "no session -> 401",
        lambda: expect(http("GET", f"{API}/accounts/me/")[0] == 401, "expected 401"),
    )

    # --- Features through the API (what the frontend calls) ------------------------------------------
    created: dict[str, str] = {}

    def create(key: str, path: str, body: dict[str, Any]) -> Callable[[], str]:
        def go() -> str:
            status, _, data = http("POST", f"{API}{path}", body, auth)
            expect(status in (200, 201), f"{status} {str(data)[:200]}")
            created[key] = str(data["id"])
            return f"id {created[key]}"

        return go

    check(
        "feature",
        "create conversation",
        create("conversation", "/conversations/", {"title": f"System {run}"}),
    )
    check(
        "feature",
        "create document",
        create("document", "/documents/", {"title": f"Spec {run}", "template": "general", "content": "v1"}),
    )
    check(
        "feature",
        "create knowledge collection",
        create("collection", "/knowledge/collections/", {"name": f"KB {run}"}),
    )
    check(
        "feature", "create dashboard", create("dashboard", "/visualize/dashboards/", {"name": f"Board {run}"})
    )

    def chat() -> str:
        expect("conversation" in created, "no conversation")
        status, _, request = http(
            "POST",
            f"{API}/chat/requests/",
            {
                "conversationId": created["conversation"],
                "chatInput": "Hello from the system check",
                "timezone": "Africa/Kampala",
                "locale": "en",
            },
            {**auth, "Idempotency-Key": str(uuid.uuid4())},
        )
        expect(status in (200, 201, 202), f"chat request {status}: {str(request)[:200]}")

        def messages() -> list[dict[str, Any]]:
            body = http("GET", f"{API}/conversations/{created['conversation']}/messages/", headers=auth)[2]
            return body if isinstance(body, list) else body.get("results", [])

        found = wait_for(
            messages,
            lambda items: any(m.get("role") == "assistant" and m.get("content") for m in items),
            90,
            "the assistant reply",
        )
        return f"{len(found)} messages; reply written by a Celery worker"

    check("feature", "chat reply through Celery", chat)

    # --- Postgres: the rows really exist ---------------------------------------------------------------
    def rows() -> str:
        ids = json.dumps(created)
        found = db(f"""
import json
from django.apps import apps
from django.contrib.auth import get_user_model
created = json.loads('{ids}')
labels = {{'conversation': 'conversations.Conversation', 'document': 'documents.Document',
          'collection': 'knowledge.Collection', 'dashboard': 'visualize.Dashboard'}}
out = {{k: apps.get_model(labels[k]).objects.filter(pk=v).exists() for k, v in created.items()}}
user = get_user_model().objects.filter(email='{email}').first()
out['user'] = bool(user)
if user:
    from apps.conversations.models import Message
    from apps.billing.models import CreditLedger, CreditWallet
    from apps.events.models import OutboxEvent
    from apps.identity.models import UserOrganization
    orgs = list(UserOrganization.objects.filter(user=user).values_list('organization_id', flat=True))
    out['organization'] = bool(orgs)
    out['wallet'] = CreditWallet.objects.filter(organization_id__in=orgs).exists()
    out['credit ledger'] = CreditLedger.objects.filter(wallet__organization_id__in=orgs).count()
    out['messages'] = Message.objects.filter(conversation_id=created.get('conversation')).count()
    out['outbox events'] = OutboxEvent.objects.filter(created_at__gte=user.date_joined).count()
print(json.dumps(out))
""")
        missing = [k for k, v in found.items() if not v]
        expect(not missing, f"missing in Postgres: {missing} ({found})")
        return ", ".join(f"{k}={v}" for k, v in found.items())

    check("postgres", "API writes are stored", rows)

    # --- Cleanup through the API (and the rows are gone) -----------------------------------------------
    paths = {
        "dashboard": "/visualize/dashboards/{}/",
        "collection": "/knowledge/collections/{}/",
        "document": "/documents/{}/",
        "conversation": "/conversations/{}/",
    }

    def cleanup() -> str:
        for key, template in paths.items():
            if key in created:
                status = http("DELETE", f"{API}{template.format(created[key])}", headers=auth)[0]
                expect(status in (200, 202, 204), f"delete {key}: {status}")
                expect(
                    http("GET", f"{API}{template.format(created[key])}", headers=auth)[0] == 404,
                    f"{key} still readable",
                )
        return f"{len(created)} records deleted"

    check("feature", "delete what was created", cleanup)

    # --- Async services ------------------------------------------------------------------------------------
    print("  … waiting 35 s for Prometheus to scrape the new counters")
    time.sleep(35)

    def grew(query: str, key: str, label: str) -> Callable[[], str]:
        def go() -> str:
            delta = prom(query) - base[key]
            expect(delta > 0, f"{label}: no change")
            return f"+{delta:.0f} {label}"

        return go

    def celery_failed() -> str:
        return (
            f"+{prom('sum(jt_celery_tasks_total{state="failed"})') - base['celery_failed']:.0f} failed tasks"
        )

    def outbox_empty() -> str:
        wait_for(lambda: prom("sum(jt_outbox_events)"), lambda v: v == 0, 60, "an empty outbox")
        return "0 pending"

    def consumer() -> str:
        lag = prom("time() - max(jt_kafka_consumer_last_poll_timestamp_seconds)")
        expect(lag < 120, f"last poll {lag:.0f}s ago")
        return f"last poll {lag:.0f}s ago"

    check(
        "celery",
        "tasks succeeded during the run",
        grew('sum(jt_celery_tasks_total{state="succeeded"})', "celery_ok", "succeeded tasks"),
    )
    check("celery", "failed tasks during the run (informational)", celery_failed)
    check(
        "kafka",
        "outbox events published",
        grew("sum(jt_outbox_published_total)", "outbox", "events published"),
    )
    check("kafka", "no event left pending", outbox_empty)
    check("kafka", "Redpanda offsets advanced", grew("sum(redpanda_kafka_max_offset)", "offsets", "offsets"))
    check("kafka", "consumer polling", consumer)
    check(
        "kafka",
        "dead-letter queue empty",
        lambda: expect(prom("sum(jt_dead_letter_events)") == 0, "dead letters present"),
    )

    def topics() -> str:
        status, _, body = http("GET", f"{CONSOLE}/api/topics")
        expect(status == 200, f"console {status}")
        names = [t["topicName"] for t in body["topics"] if t["topicName"].startswith("jt-code.")]
        expect(bool(names), "no jt-code topics")
        return f"{len(names)} jt-code topics"

    check("kafka", "topics visible in Redpanda console", topics)

    def redis_up() -> str:
        up = prom("sum(redis_up)")
        expect(up >= 2, f"redis_up={up}")
        return f"{up:.0f} instances up"

    check("redis", "cache and broker up", redis_up)
    check(
        "redis",
        "commands processed during the run",
        grew("sum(redis_commands_processed_total)", "redis_cmds", "commands"),
    )

    # --- Monitoring --------------------------------------------------------------------------------------
    def targets() -> str:
        body = http("GET", f"{PROMETHEUS}/api/v1/targets")[2]
        active = body["data"]["activeTargets"]
        down = [t["labels"].get("job") for t in active if t["health"] != "up"]
        expect(not down, f"targets down: {down}")
        return f"{len(active)} targets up"

    check("prometheus", "all scrape targets up", targets)
    check(
        "prometheus", "API request metrics moved", grew("sum(jt_http_requests_total)", "http", "API requests")
    )
    check(
        "tracing",
        "OTel collector and Tempo scraped",
        lambda: expect(prom('sum(up{job=~"otel-collector|tempo"})') >= 2, "otel-collector/tempo not up"),
    )

    grafana_auth = {
        "Authorization": "Basic "
        + base64.b64encode(f"admin:{ENV.get('GRAFANA_ADMIN_PASSWORD', '')}".encode()).decode()
    }
    check(
        "grafana",
        "health",
        lambda: expect(http("GET", f"{GRAFANA}/api/health")[2].get("database") == "ok", "unhealthy"),
    )

    def grafana_login() -> str:
        status, _, body = http("GET", f"{GRAFANA}/api/search?type=dash-db", headers=grafana_auth)
        expect(status == 200, f"admin login refused ({status}); run `make docker-grafana-reset`")
        state["dashboards"] = body
        return f"{len(body)} dashboards: " + ", ".join(sorted(d["title"] for d in body))[:140]

    check("grafana", "admin login + dashboards provisioned", grafana_login)

    def datasource() -> str:
        expect("dashboards" in state, "skipped: admin login failed (one attempt per run avoids the lockout)")
        sources = http("GET", f"{GRAFANA}/api/datasources", headers=grafana_auth)[2]
        prometheus = next((s for s in sources if s["type"] == "prometheus"), None)
        expect(prometheus is not None, "no Prometheus datasource")
        assert prometheus is not None
        uid = prometheus["uid"]
        status, _, health = http("GET", f"{GRAFANA}/api/datasources/uid/{uid}/health", headers=grafana_auth)
        expect(status == 200, f"datasource health {status}: {health}")
        return f"{len(sources)} datasources; Prometheus reachable"

    check("grafana", "Prometheus datasource works", datasource)
    check(
        "storage",
        "Supabase Storage answers",
        lambda: expect(http("GET", f"{SUPABASE}/storage/v1/status")[0] == 200, "storage status not 200"),
    )
    return report(started)


def report(started: float) -> int:
    failed = [r for r in results if not r[2]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed in {time.time() - started:.0f} s")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
