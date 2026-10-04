"""Phase 15 exit criteria: dashboards and security test gates are operational.

* Sentry events are scrubbed of personal data and credentials.
* ``/metrics`` is protected and exports every metric the Grafana dashboards
  and alert rules query (the dashboards cannot silently go blank).
* W3C trace context crosses HTTP -> outbox -> Kafka consumer.
* Security headers, client-IP resolution and the Cloudflare origin lock.
* Signed webhooks reject forged, stale and replayed requests.
* The audit pipeline records sensitive changes and denials, exports them via
  the outbox, and the database refuses to edit or delete audit rows.
* A DAST-style sweep: no operation in the OpenAPI schema answers an
  anonymous request with data or a server error.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.models import CreditWallet
from apps.core import sentry as sentry_scrub
from apps.core.signing import sign_request
from apps.events.models import OutboxEvent
from apps.governance.models import AuditEvent, RetentionRule
from apps.identity.models import Organization, Role, UserOrganization, UserRole
from apps.jobs.models import Job

ROOT = Path(settings.BASE_DIR)
pytestmark = pytest.mark.django_db


def make_org(django_user_model, label: str, *, admin: bool = True):
    owner = django_user_model.objects.create_user(
        username=f"{label}-owner", supabase_user_id=f"supabase-{label}", email=f"{label}@example.test"
    )
    organization = Organization.objects.create(name=f"{label} org", slug=f"{label}-org", owner=owner)
    UserOrganization.objects.get_or_create(user=owner, organization=organization)
    role, _ = Role.objects.get_or_create(name=Role.RoleType.ADMIN if admin else Role.RoleType.VIEWER)
    UserRole.objects.filter(user=owner, organization=organization).delete()
    UserRole.objects.create(user=owner, role=role, organization=organization)
    CreditWallet.objects.get_or_create(organization=organization, defaults={"balance": Decimal("1000")})
    return organization, owner


def client_for(user, organization, **headers) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    client.credentials(HTTP_X_ORGANIZATION_ID=str(organization.id), **headers)
    return client


def signed_headers(body: bytes, secret: str, **kwargs) -> dict[str, str]:
    return {f"HTTP_{k.upper().replace('-', '_')}": v for k, v in sign_request(body, secret, **kwargs).items()}


# Sentry ---------------------------------------------------------------------


def test_sentry_events_are_scrubbed_of_pii_and_credentials():
    token = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlX3ZhbHVl"  # pragma: allowlist secret
    )
    event = {
        "message": "failed for jane@example.com with Bearer abc.def.ghi",
        "request": {
            "method": "POST",
            "url": "https://api.example.test/api/v1/chat/?token=secret-value",
            "headers": {"Authorization": "Bearer xyz", "Cookie": "sessionid=1", "User-Agent": "pytest"},
            "cookies": {"sessionid": "1"},
            "data": {"password": "hunter2"},  # pragma: allowlist secret
            "query_string": "token=secret-value",
            "env": {"REMOTE_ADDR": "203.0.113.5"},
        },
        "user": {"id": "42", "email": "jane@example.com", "ip_address": "203.0.113.5"},
        "extra": {
            "api_key": "sk_live_1234567890abcdef",  # pragma: allowlist secret
            "note": f"jwt {token}",
            "card": "4242 4242 4242 4242",
        },
        "exception": {
            "values": [{"value": f"Bad token {token}", "stacktrace": {"frames": [{"vars": {"x": 1}}]}}]
        },
        "breadcrumbs": {
            "values": [{"message": "GET /x?api_key=abc", "data": {"url": "https://h/x?token=1"}}]
        },
    }

    cleaned = sentry_scrub.before_send(event)
    flat = json.dumps(cleaned)

    for leaked in (
        "jane@example.com",
        "hunter2",
        "secret-value",
        "xyz",
        "sessionid",
        token,
        "sk_live_123",
        "203.0.113.5",
        "4242 4242",
    ):
        assert leaked not in flat, leaked
    assert cleaned["user"] == {"id": "42"}
    assert set(cleaned["request"]["headers"]) == {"User-Agent"}
    assert "vars" not in cleaned["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert cleaned["breadcrumbs"]["values"][0]["data"]["url"] == "https://h/x"


def test_sentry_init_disables_default_pii(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(sentry_scrub.sentry_sdk, "init", lambda **kwargs: captured.update(kwargs))
    sentry_scrub.init_sentry(
        dsn="https://public@example.ingest.sentry.io/1",
        environment="production",
        release="r1",
        traces_sample_rate=0.1,
        profiles_sample_rate=0.0,
    )
    assert captured["send_default_pii"] is False
    assert captured["max_request_body_size"] == "never"
    assert captured["include_local_variables"] is False
    assert captured["before_send"] is sentry_scrub.before_send
    assert sentry_scrub.before_send_transaction({"transaction": "/api/v1/health/live/"}) is None


# Metrics and dashboards -------------------------------------------------------


def _scrape(client, token="metrics-token-0123456789abcdef0123456789"):  # pragma: allowlist secret
    return client.get("/metrics", HTTP_AUTHORIZATION=f"Bearer {token}")


def test_metrics_endpoint_requires_the_scrape_token(client, settings):
    settings.METRICS_AUTH_TOKEN = ""
    assert client.get("/metrics").status_code == 404  # not exposed without a token unless DEBUG
    settings.METRICS_AUTH_TOKEN = "metrics-token-0123456789abcdef0123456789"  # pragma: allowlist secret
    assert client.get("/metrics").status_code == 401
    assert _scrape(client, "wrong").status_code == 401
    assert _scrape(client).status_code == 200


def test_http_metrics_use_route_templates_and_state_gauges_are_exported(client, settings):
    settings.METRICS_AUTH_TOKEN = "metrics-token-0123456789abcdef0123456789"  # pragma: allowlist secret
    settings.METRICS_STATE_CACHE_SECONDS = 0
    job_id = uuid.uuid4()
    client.get(f"/api/v1/jobs/{job_id}/")
    body = _scrape(client).content.decode()

    assert 'route="/api/v1/jobs/<id>/"' in body
    assert str(job_id) not in body  # raw paths never become labels
    for gauge in (
        "jt_outbox_events",
        "jt_jobs_active",
        "jt_usage_open_reservations",
        "jt_dead_letter_events",
    ):
        assert gauge in body
    assert "jt_metrics_collector_up 1.0" in body


def _exported_metric_names() -> set[str]:
    from apps.core.metrics import render_metrics

    names = set()
    for line in render_metrics().decode().splitlines():
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ", 3)
            names.add(name)
            if kind == "counter":
                names.add(name if name.endswith("_total") else f"{name}_total")
            if kind == "histogram":
                names.update({f"{name}_bucket", f"{name}_count", f"{name}_sum"})
    return names


def _promql_expressions() -> list[tuple[str, str]]:
    expressions = []
    for path in sorted((ROOT / "infra/grafana/dashboards").glob("*.json")):
        dashboard = json.loads(path.read_text())
        assert dashboard["uid"] and dashboard["title"] and dashboard["panels"], path.name
        for panel in dashboard["panels"]:
            assert panel["targets"], f"{path.name}: {panel['title']} has no query"
            expressions.extend((f"{path.name}:{panel['title']}", t["expr"]) for t in panel["targets"])
    alerts = yaml.safe_load((ROOT / "infra/prometheus/alerts.yml").read_text())
    for group in alerts["groups"]:
        for rule in group["rules"]:
            assert rule["alert"] and rule["labels"]["severity"] in {"page", "ticket"}
            expressions.append((f"alerts:{rule['alert']}", rule["expr"]))
    return expressions


def test_dashboards_and_alerts_only_query_exported_metrics():
    exported = _exported_metric_names()
    expressions = _promql_expressions()
    assert len(expressions) > 30
    missing = {
        (where, name)
        for where, expr in expressions
        for name in re.findall(r"\b(jt_[a-z0-9_]+)", expr)
        if name not in exported
    }
    assert not missing, sorted(missing)


def test_grafana_and_prometheus_provisioning_is_well_formed():
    datasource = yaml.safe_load((ROOT / "infra/grafana/provisioning/datasources/prometheus.yml").read_text())
    provider = yaml.safe_load((ROOT / "infra/grafana/provisioning/dashboards/jt-code.yml").read_text())
    prometheus = yaml.safe_load((ROOT / "infra/prometheus/prometheus.yml").read_text())
    collector = yaml.safe_load((ROOT / "infra/otel/collector.yaml").read_text())
    assert {d["uid"] for d in datasource["datasources"]} >= {"prometheus"}
    assert provider["providers"][0]["options"]["path"]
    api_job = next(job for job in prometheus["scrape_configs"] if job["job_name"] == "jt-code-api")
    assert api_job["authorization"]["credentials_file"]  # the token is never inlined
    assert "attributes/scrub" in collector["service"]["pipelines"]["traces"]["processors"]
    uids = [json.loads(p.read_text())["uid"] for p in (ROOT / "infra/grafana/dashboards").glob("*.json")]
    assert len(uids) == len(set(uids)) == 4


# Tracing --------------------------------------------------------------------


def _in_memory_tracer():
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return trace.get_tracer("test"), exporter


def test_trace_context_flows_from_request_through_outbox_to_consumer(client):
    from apps.core.tracing import span_from_headers
    from apps.events.outbox import enqueue_outbox_event

    tracer, exporter = _in_memory_tracer()
    with tracer.start_as_current_span("http request") as span:
        trace_id = format(span.get_span_context().trace_id, "032x")
        response = client.get("/api/v1/health/live/")
        event = enqueue_outbox_event("jobs.job.created", "k", {"job_id": "1"})

    # Logs, responses and events share the OpenTelemetry trace id.
    assert response["X-Trace-ID"] == trace_id
    assert event.headers["traceparent"].split("-")[1] == trace_id

    with span_from_headers("consume jobs.job.created", event.headers) as consumer:
        assert format(consumer.get_span_context().trace_id, "032x") == trace_id
    consumer_span = next(s for s in exporter.get_finished_spans() if s.name == "consume jobs.job.created")
    assert (
        consumer_span.parent is not None and consumer_span.parent.span_id == span.get_span_context().span_id
    )


def test_tracing_installs_sdk_and_instrumentation_when_an_endpoint_is_configured():
    code = (
        "import os, django; django.setup();"
        "from django.conf import settings; from opentelemetry import trace;"
        "from opentelemetry.sdk.trace import TracerProvider;"
        "assert isinstance(trace.get_tracer_provider(), TracerProvider);"
        "assert any('opentelemetry' in m for m in settings.MIDDLEWARE), settings.MIDDLEWARE;"
        "print('traced', flush=True); os._exit(0)"
    )
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0 and "traced" in result.stdout, result.stderr[-2000:]


# Headers, client IP and the origin lock ---------------------------------------


def test_api_responses_carry_security_headers(client, authenticated_client):
    response = client.get("/api/v1/health/live/")
    assert response["Content-Security-Policy"].startswith("default-src 'none'")
    assert "frame-ancestors 'none'" in response["Content-Security-Policy"]
    assert "camera=()" in response["Permissions-Policy"]
    assert response["Cross-Origin-Resource-Policy"] == "same-site"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["X-Frame-Options"] == "DENY"
    assert authenticated_client.get("/api/v1/me/")["Cache-Control"] == "no-store"
    assert "script-src 'self'" in client.get("/api/docs/")["Content-Security-Policy"]


def test_client_ip_ignores_spoofable_headers_unless_proxies_are_trusted(rf, settings):
    from apps.core.edge import client_ip

    settings.CLOUDFLARE_ORIGIN_SECRET = "o" * 40
    spoofed = rf.get(
        "/",
        HTTP_X_FORWARDED_FOR="1.2.3.4, 203.0.113.9",
        HTTP_CF_CONNECTING_IP="9.9.9.9",
        REMOTE_ADDR="10.0.0.2",
    )
    settings.TRUSTED_PROXY_HOPS = 0
    assert client_ip(spoofed) == "10.0.0.2"
    settings.TRUSTED_PROXY_HOPS = 1
    spoofed.__dict__.pop("_jt_client_ip", None)
    assert client_ip(spoofed) == "203.0.113.9"  # the hop our proxy appended, not the client's claim
    via_cf = rf.get("/", HTTP_CF_CONNECTING_IP="198.51.100.7", HTTP_X_JT_ORIGIN_AUTH="o" * 40)
    assert client_ip(via_cf) == "198.51.100.7"


def test_origin_lock_rejects_requests_that_bypass_cloudflare(client, settings):
    settings.CLOUDFLARE_ORIGIN_SECRET = "o" * 40
    settings.CLOUDFLARE_ENFORCE_ORIGIN = True
    assert client.get("/api/v1/plans/").status_code == 403
    assert client.get("/api/v1/health/live/").status_code == 200  # load-balancer probes stay open
    assert client.get("/api/v1/plans/", HTTP_X_JT_ORIGIN_AUTH="o" * 40).status_code != 403


def test_ip_rate_limit_keys_on_the_resolved_client_ip(authenticated_client, settings):
    rates = {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "ip": "2/minute"}
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": rates}
    settings.TRUSTED_PROXY_HOPS = 1
    first = [
        authenticated_client.get("/api/v1/me/", HTTP_X_FORWARDED_FOR="198.51.100.1").status_code
        for _ in range(3)
    ]
    other = authenticated_client.get("/api/v1/me/", HTTP_X_FORWARDED_FOR="198.51.100.2").status_code
    assert first[-1] == 429
    assert other != 429  # clients behind the same proxy no longer share one bucket


# Webhook replay protection -------------------------------------------------------


@pytest.fixture
def n8n_job(django_user_model, settings):
    settings.N8N_WEBHOOK_SECRET = "n8n-callback-secret-0123456789abcdef"  # pragma: allowlist secret
    settings.N8N_WEBHOOK_SECRET_PREVIOUS = "n8n-previous-secret-0123456789abcdef"  # pragma: allowlist secret
    organization, owner = make_org(django_user_model, "replay")
    return Job.objects.create(
        owner=owner, organization=organization, task_type=Job.TaskType.SCHEDULED_AUTOMATION, input_payload={}
    )


def _callback(job, body: bytes, headers: dict):
    return APIClient().generic(
        "POST", f"/api/v1/jobs/{job.id}/status/", data=body, content_type="application/json", **headers
    )


def test_signed_callbacks_reject_forged_stale_and_replayed_requests(n8n_job, settings):
    body = json.dumps({"status": "running", "progress_percent": 10}).encode()
    headers = signed_headers(body, settings.N8N_WEBHOOK_SECRET)

    assert _callback(n8n_job, body, headers).status_code == 200
    assert _callback(n8n_job, body, headers).status_code == 409  # replayed nonce
    assert _callback(n8n_job, body, signed_headers(body, "forged-secret-0123456789abcdef")).status_code == 401
    stale = signed_headers(body, settings.N8N_WEBHOOK_SECRET, timestamp=int(time.time()) - 3600)
    assert _callback(n8n_job, body, stale).status_code == 401
    tampered = signed_headers(body, settings.N8N_WEBHOOK_SECRET)
    assert _callback(n8n_job, body.replace(b"10", b"99"), tampered).status_code == 401
    assert _callback(n8n_job, body, {}).status_code == 401
    rotated = signed_headers(body, settings.N8N_WEBHOOK_SECRET_PREVIOUS)
    assert _callback(n8n_job, body, rotated).status_code == 200  # previous secret during rotation

    rejected = AuditEvent.objects.filter(action="webhook.rejected", organization=None)
    assert rejected.count() >= 4
    assert set(rejected.values_list("outcome", flat=True)) == {AuditEvent.Outcome.DENIED}


def test_unconfigured_secret_fails_closed(n8n_job, settings):
    settings.N8N_WEBHOOK_SECRET = ""
    settings.N8N_WEBHOOK_SECRET_PREVIOUS = ""
    body = b'{"status": "running"}'
    assert _callback(n8n_job, body, signed_headers(body, "anything-0123456789abcdef")).status_code == 503


def test_n8n_error_relay_is_signed_and_reaches_sentry(monkeypatch, settings):
    import sentry_sdk

    settings.N8N_SENTRY_RELAY_SECRET = "relay-secret-0123456789abcdef"  # pragma: allowlist secret
    captured = []
    monkeypatch.setattr(sentry_sdk, "capture_message", lambda message, level=None: captured.append(message))
    body = json.dumps({"message": "Workflow failed", "workflowId": "wf-1", "executionId": "9"}).encode()
    url = "/api/v1/monitoring/n8n-error/"
    unsigned = APIClient().generic("POST", url, data=body, content_type="application/json")
    signed = APIClient().generic(
        "POST",
        url,
        data=body,
        content_type="application/json",
        **signed_headers(body, settings.N8N_SENTRY_RELAY_SECRET),
    )
    assert unsigned.status_code == 401
    assert signed.status_code == 202
    assert captured == ["Workflow failed"]


def test_forged_stripe_webhook_is_audited():
    response = APIClient().post(
        "/api/v1/webhooks/stripe/", "{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="t=1,v1=bad"
    )
    assert response.status_code == 400
    assert AuditEvent.objects.filter(action="webhook.rejected", resource_type="stripe").exists()


# Audit pipeline ---------------------------------------------------------------


def test_sensitive_changes_are_audited_and_exported_through_the_outbox(django_user_model):
    organization, owner = make_org(django_user_model, "audit")
    client = client_for(owner, organization, HTTP_USER_AGENT="audit-test", REMOTE_ADDR="203.0.113.20")

    response = client.post("/api/v1/api-keys/", {"name": "CI key"}, format="json")
    assert response.status_code == 201, response.content

    event = AuditEvent.objects.get(organization=organization, category="configuration")
    assert event.actor == owner
    assert event.outcome == AuditEvent.Outcome.SUCCESS
    assert event.ip_address == "203.0.113.20"
    assert event.metadata["route"].startswith("/api/v1/api-keys/")
    exported = OutboxEvent.objects.get(
        topic__endswith="governance.audit.recorded", payload__audit_event_id=str(event.id)
    )
    assert exported.payload["organization_id"] == str(organization.id)
    assert response.json()["key"] not in json.dumps(event.metadata)  # the secret is never logged


def add_viewer(django_user_model, organization, label: str):
    viewer = django_user_model.objects.create_user(
        username=label, supabase_user_id=f"sb-{label}", email=f"{label}@example.test"
    )
    UserOrganization.objects.create(user=viewer, organization=organization)
    UserRole.objects.filter(user=viewer, organization=organization).delete()
    role, _ = Role.objects.get_or_create(name=Role.RoleType.VIEWER)
    UserRole.objects.create(user=viewer, role=role, organization=organization)
    return viewer


def test_denied_attempts_on_sensitive_routes_are_audited(django_user_model):
    organization, _ = make_org(django_user_model, "denied")
    viewer = add_viewer(django_user_model, organization, "denied-viewer")
    response = client_for(viewer, organization).post(
        "/api/v1/tool-policies/", {"tool_name": "x"}, format="json"
    )
    assert response.status_code == 403
    event = AuditEvent.objects.get(organization=organization, outcome=AuditEvent.Outcome.DENIED)
    assert event.category == AuditEvent.Category.AUTHORIZATION
    assert event.actor == viewer


def test_audit_rows_are_append_only_in_the_database(django_user_model):
    organization, owner = make_org(django_user_model, "immutable")
    from apps.governance.audit import record_audit_event

    event = record_audit_event(
        category="security",
        action="probe",
        resource_type="test",
        organization=organization,
        actor=owner,
        metadata={"api_token": "t0p"},
    )
    assert event.metadata == {"api_token": "[redacted]"}
    with pytest.raises(IntegrityError), transaction.atomic():
        AuditEvent.objects.filter(pk=event.pk).update(description="tampered")
    with pytest.raises(IntegrityError), transaction.atomic():
        AuditEvent.objects.filter(pk=event.pk).delete()

    # Deleting the tenant (an explicit, audited operation) purges its trail.
    organization.delete()
    assert not AuditEvent.objects.filter(pk=event.pk).exists()


def test_actor_deletion_keeps_the_audit_event(django_user_model):
    organization, owner = make_org(django_user_model, "actor-delete")
    member = django_user_model.objects.create_user(
        username="leaver", supabase_user_id="sb-leaver", email="leaver@example.test"
    )
    from apps.governance.audit import record_audit_event

    event = record_audit_event(
        category="admin", action="probe", resource_type="test", organization=organization, actor=member
    )
    member.delete()
    event.refresh_from_db()
    assert event.actor_id is None


def test_retention_deletes_anonymizes_and_respects_legal_hold(django_user_model, settings):
    from apps.governance.audit import record_audit_event
    from apps.governance.tasks import cleanup_old_audit_events

    settings.AUDIT_EVENT_RETENTION_DAYS = 30
    orgs = {
        name: make_org(django_user_model, f"ret-{name}") for name in ("delete", "anon", "hold", "default")
    }
    events = {}
    for name, (organization, owner) in orgs.items():
        events[name] = record_audit_event(
            category="admin", action="old", resource_type="t", organization=organization, actor=owner
        )
    old = timezone.now() - timedelta(days=400)
    with transaction.atomic():
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('jt_code.ledger_purge', 'on', true)")
        AuditEvent.objects.filter(pk__in=[e.pk for e in events.values()]).update(created_at=old)
    for name, action, hold in (
        ("delete", "hard_delete", False),
        ("anon", "anonymize", False),
        ("hold", "hard_delete", True),
    ):
        RetentionRule.objects.create(
            organization=orgs[name][0],
            data_category="audit_events",
            action=action,
            retention_days=90,
            grace_period_days=0,
            legal_hold=hold,
        )

    cleanup_old_audit_events()

    assert not AuditEvent.objects.filter(pk=events["delete"].pk).exists()
    assert not AuditEvent.objects.filter(pk=events["default"].pk).exists()
    assert AuditEvent.objects.filter(pk=events["hold"].pk).exists()
    anonymized = AuditEvent.objects.get(pk=events["anon"].pk)
    assert anonymized.actor_id is None and anonymized.ip_address is None


def test_audit_log_api_is_admin_only_and_validates_filters(django_user_model):
    organization, owner = make_org(django_user_model, "auditlog")
    viewer = add_viewer(django_user_model, organization, "auditlog-viewer")

    assert client_for(owner, organization).get("/api/v1/audit-events/").status_code == 200
    assert client_for(viewer, organization).get("/api/v1/audit-events/").status_code == 403
    bad = client_for(owner, organization).get("/api/v1/audit-events/?start_date=yesterday")
    assert bad.status_code == 400


def test_admin_changes_are_audited(django_user_model, admin_user):
    from django.contrib.admin.models import CHANGE, LogEntry
    from django.contrib.contenttypes.models import ContentType

    LogEntry.objects.create(
        user=admin_user,
        content_type=ContentType.objects.get_for_model(Organization),
        object_id="1",
        object_repr="Org",
        action_flag=CHANGE,
        change_message="Changed name.",
    )
    assert AuditEvent.objects.filter(category="admin", action="admin.change", actor=admin_user).exists()


# Security gates ---------------------------------------------------------------


def test_zap_report_policy_fails_on_fail_rules_and_unlisted_high_risk():
    from apps.core.management.commands.security_gate import parse_rules, zap_failures

    rules = parse_rules(ROOT / ".zap/rules.tsv")
    assert rules["40018"] == "FAIL" and rules["10202"] == "IGNORE"
    report = {
        "site": [
            {
                "alerts": [
                    {"pluginid": "10202", "name": "Absence of Anti-CSRF Tokens", "riskcode": "1"},
                    {"pluginid": "10096", "name": "Timestamp Disclosure", "riskcode": "0"},
                ]
            }
        ]
    }
    assert zap_failures(report, rules) == []
    report["site"][0]["alerts"].append({"pluginid": "40018", "name": "SQL Injection", "riskcode": "3"})
    report["site"][0]["alerts"].append({"pluginid": "99999", "name": "New critical", "riskcode": "3"})
    assert len(zap_failures(report, rules)) == 2


# Anonymous access to non-public operations, by design.
PUBLIC_OPERATIONS = {
    ("GET", "/api/v1/health/live/"),
    ("GET", "/api/v1/health/ready/"),
    ("GET", "/api/v1/health/startup/"),
}


def test_dast_sweep_no_operation_serves_anonymous_data_or_errors(client):
    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator(api_version="v1").get_schema(request=None, public=True)
    checked = 0
    failures = []
    for path, operations in schema["paths"].items():
        concrete = re.sub(r"\{(id|pk|job_id|asset_id|document_id|webhook_id)\}", str(uuid.uuid4()), path)
        concrete = re.sub(r"\{[^}]+\}", "missing", concrete)
        for method in operations:
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            if method in {"get", "delete"}:
                response = getattr(client, method)(concrete)
            else:
                response = getattr(client, method)(concrete, data="{}", content_type="application/json")
            checked += 1
            status = response.status_code
            if status >= 500:
                failures.append(f"{method.upper()} {path} -> {status}")
            elif status < 300 and (method.upper(), path) not in PUBLIC_OPERATIONS:
                failures.append(f"{method.upper()} {path} answered anonymously with {status}")
            if (
                "Content-Security-Policy" not in response
                or response.get("X-Content-Type-Options") != "nosniff"
            ):
                failures.append(f"{method.upper()} {path} is missing security headers")
    assert checked > 150
    assert not failures, failures
