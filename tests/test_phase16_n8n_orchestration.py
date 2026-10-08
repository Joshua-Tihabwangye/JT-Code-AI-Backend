"""Phase 16 exit criteria: workflow retries and callback security tests pass.

n8n is replaced by an ``httpx.MockTransport`` that verifies every Django->n8n
request exactly as the workflows' "Verify JT-Code signature" node does, and
the tests then call back with signed requests like the workflows'
"Sign ..." nodes. Covered touchpoints: job workflows, tenant automations,
event notifications, knowledge integration sync, integration checks, the error
workflow, workflow events (Kafka) and versioned definitions (push/drift).
"""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from django.conf import settings as django_settings
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.models import CreditWallet
from apps.core.signing import SignatureError, sign_request, verify_request
from apps.events.models import OutboxEvent
from apps.events.outbox import enqueue_outbox_event
from apps.governance.models import AuditEvent
from apps.identity.models import Organization, Role, UserOrganization, UserRole
from apps.jobs.models import Job, WorkflowRun
from apps.orchestration import client as n8n_client
from apps.orchestration import deliveries, runs
from apps.orchestration.models import Automation, WorkflowDefinition, WorkflowEventDelivery
from apps.orchestration.registry import (
    RegistryError,
    invalidate_routing,
    load_specs,
    register_specs,
    validate_definition,
)
from apps.usage.models import UsageReservation

pytestmark = pytest.mark.django_db
DISPATCH = "dispatch-secret-0123456789abcdef0123"  # pragma: allowlist secret
CALLBACK = "callback-secret-0123456789abcdef0123"  # pragma: allowlist secret
RELAY = "relay-secret-0123456789abcdef012345"  # pragma: allowlist secret
API = "https://api.example.test/api/v1"


class FakeN8n:
    """Scripted n8n: verifies signatures like the workflows do and records requests."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responses: dict[str, list] = {}
        self.workflows: dict[str, dict] = {}

    def script(self, path_fragment: str, *responses) -> None:
        self.responses.setdefault(path_fragment, []).extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/v1/" in url:
            return self.admin(request)
        body = request.content
        headers = request.headers  # case-insensitive, like n8n's lower-cased headers
        try:
            verify_request(body=body, headers=headers, secrets_=[DISPATCH], namespace="fake-n8n")
        except SignatureError as exc:
            return httpx.Response(401, json={"accepted": False, "reason": exc.code})
        payload = json.loads(body)
        self.requests.append({"url": url, "payload": payload, "headers": headers})
        for fragment, queue in self.responses.items():
            if fragment in url and queue:
                response = queue.pop(0)
                if isinstance(response, Exception):
                    raise response
                status, data = response
                return httpx.Response(status, json=data)
        return httpx.Response(202, json={"accepted": True, "executionId": str(len(self.requests))})

    def admin(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-N8N-API-KEY"] == "n8n-api-key"
        path = request.url.path.split("/api/v1", 1)[1]
        if request.method == "GET" and path == "/workflows":
            return httpx.Response(200, json={"data": list(self.workflows.values()), "nextCursor": None})
        if request.method == "POST" and path == "/workflows":
            body = json.loads(request.content)
            assert set(body) <= {"name", "nodes", "connections", "settings", "staticData"}
            workflow = {**body, "id": f"wf{len(self.workflows) + 1}", "active": False}
            self.workflows[workflow["id"]] = workflow
            return httpx.Response(200, json=workflow)
        workflow_id = path.split("/")[2]
        workflow = self.workflows[workflow_id]
        if request.method == "PUT":
            workflow.update(json.loads(request.content))
        elif path.endswith("/activate"):
            workflow["active"] = True
        elif path.endswith("/deactivate"):
            workflow["active"] = False
        return httpx.Response(200, json=workflow)


@pytest.fixture
def n8n(settings, monkeypatch):
    settings.N8N_BASE_URL = "https://n8n.example.test"
    settings.N8N_WEBHOOK_BASE_URL = "https://hooks.example.test"
    settings.N8N_API_KEY = "n8n-api-key"  # pragma: allowlist secret
    settings.N8N_DISPATCH_SECRET = DISPATCH
    settings.N8N_WEBHOOK_SECRET = CALLBACK
    settings.N8N_WEBHOOK_SECRET_PREVIOUS = ""
    settings.N8N_SENTRY_RELAY_SECRET = RELAY
    settings.N8N_CALLBACK_BASE_URL = API
    settings.N8N_RETRY_BASE_SECONDS = 30
    settings.N8N_RETRY_MAX_SECONDS = 600
    settings.N8N_CREDENTIAL_IDS = {
        "N8N_CREDENTIAL_SLACK": "c-slack",
        "N8N_CREDENTIAL_SMTP": "c-smtp",
        "N8N_CREDENTIAL_GOOGLE_DRIVE": "c-drive",
        "N8N_CREDENTIAL_NOTION": "c-notion",
        "N8N_CREDENTIAL_GITHUB": "c-github",
    }
    cache.clear()
    invalidate_routing()
    fake = FakeN8n()
    monkeypatch.setattr(
        n8n_client, "http_client", lambda **kw: httpx.Client(transport=httpx.MockTransport(fake.handler))
    )
    return fake


def make_org(django_user_model, label: str):
    owner = django_user_model.objects.create_user(
        username=f"{label}-owner", supabase_user_id=f"sb-{label}", email=f"{label}@example.test"
    )
    organization = Organization.objects.create(name=f"{label} org", slug=f"{label}-org", owner=owner)
    UserOrganization.objects.get_or_create(user=owner, organization=organization)
    role, _ = Role.objects.get_or_create(name=Role.RoleType.ADMIN)
    UserRole.objects.get_or_create(user=owner, role=role, organization=organization)
    CreditWallet.objects.update_or_create(organization=organization, defaults={"balance": Decimal("100000")})
    return organization, owner


def client_for(user, organization) -> APIClient:
    api = APIClient()
    api.force_authenticate(user)
    api.credentials(HTTP_X_ORGANIZATION_ID=str(organization.id))
    return api


def signed(path: str, body: dict, *, secret: str = CALLBACK, method: str = "POST", **sign_kwargs):
    raw = json.dumps(body).encode() if body is not None else b""
    headers = {
        f"HTTP_{k.upper().replace('-', '_')}": v for k, v in sign_request(raw, secret, **sign_kwargs).items()
    }
    return APIClient().generic(
        method, f"/api/v1/{path}", data=raw, content_type="application/json", **headers
    )


def automation_job(django_user_model, django_capture_on_commit_callbacks, label="auto"):
    organization, owner = make_org(django_user_model, label)
    api = client_for(owner, organization)
    with django_capture_on_commit_callbacks(execute=True):
        response = api.post(
            "/api/v1/jobs/",
            {
                "idempotency_key": f"{label}-1",
                "task_type": "SCHEDULED_AUTOMATION",
                "input_payload": {
                    "action": "webhook",
                    "url": "https://callbacks.example.test/hook",
                    "payload": {},
                },
            },
            format="json",
        )
    assert response.status_code == 201, response.content
    job = Job.objects.get(idempotency_key=f"{label}-1")
    return job, WorkflowRun.objects.get(job=job), organization, owner


def make_due(run):
    WorkflowRun.objects.filter(id=run.id).update(next_attempt_at=timezone.now() - timedelta(seconds=1))


# Versioned definitions ---------------------------------------------------------


def test_committed_definitions_satisfy_the_contract():
    specs = {spec.key: spec for spec in load_specs()}
    assert set(specs) == {
        "error-handler",
        "scheduled-automation",
        "event-notifications",
        "knowledge-integration-sync",
        "integration-test",
    }
    assert specs["scheduled-automation"].task_types == ("SCHEDULED_AUTOMATION",)
    assert "knowledge.integration.sync_requested" in specs["knowledge-integration-sync"].event_types
    assert WorkflowDefinition.objects.filter(is_active=True).count() == 5  # registered on migrate


def _definition(name: str) -> tuple[Path, dict]:
    path = Path(django_settings.BASE_DIR) / "n8n/workflows" / name
    return path, json.loads(path.read_text())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["settings"].update(saveDataSuccessExecution="all"), "saveDataSuccessExecution"),
        (lambda d: d["meta"]["jtCode"].update(eventTypes=["orchestration.workflow.failed"]), "feedback loop"),
        (lambda d: d["nodes"][0]["parameters"].update(path="other/path"), "webhook"),
        (
            lambda d: next(n for n in d["nodes"] if n.get("credentials")).update(
                credentials={"slackApi": {"id": "17", "name": "x"}}
            ),
            "N8N_CREDENTIAL",
        ),
        (
            lambda d: d.update(
                nodes=[n for n in d["nodes"] if n["name"] != "Verify JT-Code signature"], connections={}
            ),
            "Verify JT-Code signature",
        ),
    ],
)
def test_contract_violations_are_rejected(mutate, message):
    path, data = _definition("event-notifications.v1.json")
    mutate(data)
    with pytest.raises(RegistryError, match=message):
        validate_definition(path, data)


def test_a_version_is_immutable_and_the_newest_version_is_dispatched(tmp_path):
    path, data = _definition("event-notifications.v1.json")
    changed = copy.deepcopy(data)
    changed["nodes"][1]["parameters"]["jsCode"] += "\n// edited"
    (tmp_path / path.name).write_text(json.dumps(changed))
    with pytest.raises(RegistryError, match="bump the version"):
        register_specs(load_specs(tmp_path))

    latest = WorkflowDefinition.objects.filter(key="event-notifications").order_by("-version").first().version
    nxt = latest + 1
    newer = copy.deepcopy(changed)
    newer["name"] = f"jt-code.event-notifications.v{nxt}"
    newer["meta"]["jtCode"].update(version=nxt, webhookPath=f"jt-code/event-notifications/v{nxt}")
    newer["nodes"][0]["parameters"]["path"] = f"jt-code/event-notifications/v{nxt}"
    (tmp_path / path.name).write_text(json.dumps(data))
    (tmp_path / f"event-notifications.v{nxt}.json").write_text(json.dumps(newer))
    register_specs(load_specs(tmp_path))
    active = WorkflowDefinition.objects.get(key="event-notifications", is_active=True)
    assert active.version == nxt
    assert WorkflowDefinition.objects.get(key="event-notifications", version=1).is_active is False


# Job workflows: signed dispatch and retries ---------------------------------------


def test_dispatch_is_signed_and_carries_callbacks(n8n, django_user_model, django_capture_on_commit_callbacks):
    job, run, organization, _ = automation_job(django_user_model, django_capture_on_commit_callbacks)

    request = n8n.requests[-1]
    assert request["url"] == "https://hooks.example.test/webhook/jt-code/scheduled-automation/v1"
    assert request["headers"]["Idempotency-Key"] == f"{run.id}:1"
    payload = request["payload"]
    assert payload["runId"] == str(run.id) and payload["attempt"] == 1
    assert payload["organizationId"] == str(organization.id)
    assert payload["callbacks"]["status"] == f"{API}/n8n/runs/{run.id}/status/"
    run.refresh_from_db()
    job.refresh_from_db()
    assert run.status == WorkflowRun.Status.RUNNING and run.n8n_execution_id == "1"
    assert job.status == Job.Status.RUNNING
    assert OutboxEvent.objects.filter(topic__endswith="orchestration.workflow.dispatched").exists()


def test_unsigned_dispatch_would_be_rejected_by_the_workflow(
    n8n, settings, django_user_model, django_capture_on_commit_callbacks
):
    settings.N8N_DISPATCH_SECRET = "a-different-secret-than-n8n-has-0123"  # pragma: allowlist secret
    job, _, _, _ = automation_job(django_user_model, django_capture_on_commit_callbacks)
    # n8n answers 401 (bad signature): a permanent rejection; nothing was executed.
    assert n8n.requests == []
    job.refresh_from_db()
    assert job.status == Job.Status.FAILED and job.error_code == "N8N_DISPATCH_FAILED"


def test_transient_dispatch_failures_are_retried_with_backoff(
    n8n, django_user_model, django_capture_on_commit_callbacks
):
    n8n.script("scheduled-automation", (503, {}), httpx.ConnectTimeout("timeout"))
    job, run, _, _ = automation_job(django_user_model, django_capture_on_commit_callbacks)

    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.PENDING and run.attempt == 1
    first_delay = (run.next_attempt_at - timezone.now()).total_seconds()
    assert 25 <= first_delay <= 40
    assert runs.dispatch_run(run.id) == "not_due"  # backoff is respected

    make_due(run)
    assert runs.sweep_runs()["dispatched"] == 1
    run.refresh_from_db()
    assert run.attempt == 2 and run.status == WorkflowRun.Status.PENDING
    assert (run.next_attempt_at - timezone.now()).total_seconds() > first_delay  # exponential

    make_due(run)
    runs.sweep_runs()
    run.refresh_from_db()
    job.refresh_from_db()
    assert run.attempt == 3 and run.status == WorkflowRun.Status.RUNNING
    assert job.status == Job.Status.RUNNING
    # Each attempt has its own idempotency key; the first two failed (503, timeout).
    assert [r["headers"]["Idempotency-Key"] for r in n8n.requests] == [f"{run.id}:{n}" for n in (1, 2, 3)]
    assert OutboxEvent.objects.filter(topic__endswith="orchestration.workflow.retry_scheduled").count() == 2


def test_exhausted_retries_fail_the_job_and_release_the_hold(
    n8n, django_user_model, django_capture_on_commit_callbacks
):
    n8n.script("scheduled-automation", *[(500, {})] * 5)
    job, run, _, _ = automation_job(django_user_model, django_capture_on_commit_callbacks)
    for _ in range(run.max_attempts - 1):
        make_due(run)
        runs.sweep_runs()
    job.refresh_from_db()
    run.refresh_from_db()
    assert run.attempt == run.max_attempts == 5
    assert job.status == Job.Status.FAILED and job.error_code == "N8N_DISPATCH_FAILED"
    reservation = UsageReservation.objects.get(source_type="job", source_id=str(job.id))
    assert reservation.status == UsageReservation.Status.RELEASED
    assert OutboxEvent.objects.filter(topic__endswith="jobs.job.failed").exists()


def test_permanent_rejections_are_not_retried(n8n, django_user_model, django_capture_on_commit_callbacks):
    n8n.script("scheduled-automation", (400, {"message": "bad input"}))
    job, run, _, _ = automation_job(django_user_model, django_capture_on_commit_callbacks)
    job.refresh_from_db()
    assert job.status == Job.Status.FAILED
    assert WorkflowRun.objects.get(id=run.id).attempt == 1


def test_silent_executions_time_out_and_are_retried(
    n8n, django_user_model, django_capture_on_commit_callbacks
):
    _, run, _, _ = automation_job(django_user_model, django_capture_on_commit_callbacks)
    WorkflowRun.objects.filter(id=run.id).update(deadline_at=timezone.now() - timedelta(seconds=1))
    assert runs.sweep_runs()["timed_out"] == 1
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.PENDING and run.last_error_code == "N8N_TIMEOUT"


# Callback security ----------------------------------------------------------------


def test_signed_completion_callback_completes_the_job(
    n8n, django_user_model, django_capture_on_commit_callbacks
):
    job, run, _, _ = automation_job(django_user_model, django_capture_on_commit_callbacks)
    body = {"attempt": 1, "status": "completed", "result": {"delivered": True}, "executionId": "1"}
    response = signed(f"n8n/runs/{run.id}/status/", body)
    assert response.status_code == 200, response.content
    job.refresh_from_db()
    assert job.status == Job.Status.COMPLETED and job.result == {"delivered": True}
    assert WorkflowRun.objects.get(id=run.id).status == WorkflowRun.Status.COMPLETED
    assert OutboxEvent.objects.filter(topic__endswith="orchestration.workflow.completed").exists()
    assert UsageReservation.objects.get(source_id=str(job.id)).status != UsageReservation.Status.HELD


def test_callbacks_reject_forgery_replay_staleness_and_wrong_direction(
    n8n, django_user_model, django_capture_on_commit_callbacks
):
    job, run, _, _ = automation_job(django_user_model, django_capture_on_commit_callbacks)
    path = f"n8n/runs/{run.id}/status/"
    body = {"attempt": 1, "status": "running", "progress": 40}
    raw = json.dumps(body).encode()
    headers = {f"HTTP_{k.upper().replace('-', '_')}": v for k, v in sign_request(raw, CALLBACK).items()}

    def send(extra_headers, payload=raw):
        return APIClient().generic(
            "POST", f"/api/v1/{path}", data=payload, content_type="application/json", **extra_headers
        )

    assert send(headers).status_code == 200
    assert send(headers).status_code == 409  # replayed nonce (cache)
    cache.clear()
    assert send(headers).json() == {"duplicate": True}  # durable dedupe survives cache loss
    assert send({}).status_code == 401  # unsigned
    forged = "forged-secret-0123456789abcdef"  # pragma: allowlist secret
    assert signed(path, body, secret=forged).status_code == 401
    assert signed(path, body, secret=DISPATCH).status_code == 401  # Django->n8n secret is not accepted
    stale = signed(path, body, timestamp=int(timezone.now().timestamp()) - 3600)
    assert stale.status_code == 401
    tampered = {k: v for k, v in headers.items()}
    tampered["HTTP_X_JT_CODE_NONCE"] = "a-new-nonce-value-1234567890"
    assert send(tampered).status_code == 401  # nonce is covered by the signature
    assert signed(path, {**body, "attempt": 2}).status_code == 409  # superseded attempt
    assert AuditEvent.objects.filter(action="webhook.rejected", resource_type="n8n").count() >= 4
    job.refresh_from_db()
    assert job.status == Job.Status.RUNNING and job.progress_percent == 40


def test_callbacks_cannot_change_a_cancelled_job(n8n, django_user_model, django_capture_on_commit_callbacks):
    job, run, organization, owner = automation_job(django_user_model, django_capture_on_commit_callbacks)
    assert client_for(owner, organization).post(f"/api/v1/jobs/{job.id}/cancel/").status_code == 200
    response = signed(f"n8n/runs/{run.id}/status/", {"attempt": 1, "status": "completed", "result": {}})
    assert response.status_code == 409
    state = signed(f"n8n/runs/{run.id}/", None, method="GET")
    assert state.status_code == 200 and state.json()["cancelled"] is True
    assert Job.objects.get(id=job.id).status == Job.Status.CANCELLED


def test_failed_callbacks_retry_unless_permanent(n8n, django_user_model, django_capture_on_commit_callbacks):
    job, run, _, _ = automation_job(django_user_model, django_capture_on_commit_callbacks)
    retry = signed(
        f"n8n/runs/{run.id}/status/",
        {"attempt": 1, "status": "failed", "error": {"code": "SMTP_DOWN", "message": "smtp"}},
    )
    assert retry.json()["outcome"] == "retry_scheduled"
    make_due(run)
    runs.sweep_runs()
    final = signed(
        f"n8n/runs/{run.id}/status/",
        {
            "attempt": 2,
            "status": "failed",
            "error": {"code": "BAD_INPUT", "message": "no", "retryable": False},
        },
    )
    assert final.json()["outcome"] == "failed"
    job.refresh_from_db()
    assert job.status == Job.Status.FAILED and job.error_code == "BAD_INPUT"


def test_error_workflow_relays_to_sentry_and_retries_the_execution(
    n8n, monkeypatch, django_user_model, django_capture_on_commit_callbacks
):
    import sentry_sdk

    captured = []
    monkeypatch.setattr(sentry_sdk, "capture_message", lambda message, level=None: captured.append(message))
    _, run, _, _ = automation_job(django_user_model, django_capture_on_commit_callbacks)
    body = {
        "message": "Slack node failed",
        "workflowName": "jt-code.scheduled-automation.v1",
        "executionId": "1",
    }
    assert signed("n8n/errors/", body, secret=CALLBACK).status_code == 401  # relay has its own secret
    response = signed("n8n/errors/", body, secret=RELAY)
    assert response.status_code == 202 and response.json()["retry"] == "retry_scheduled"
    assert captured == ["Slack node failed"]
    assert OutboxEvent.objects.filter(topic__endswith="orchestration.workflow.error").exists()
    assert WorkflowRun.objects.get(id=run.id).last_error_code == "N8N_EXECUTION_ERROR"


def test_workflow_events_are_published_to_kafka_through_the_outbox(n8n):
    response = signed("n8n/events/", {"name": "invoice_sent", "key": "inv-1", "data": {"amount": 10}})
    assert response.status_code == 202
    event = OutboxEvent.objects.get(id=response.json()["eventId"])
    assert event.topic.endswith("orchestration.n8n.invoice_sent") and event.payload["data"] == {"amount": 10}


def test_n8n_disabled_fails_fast(settings, django_user_model, django_capture_on_commit_callbacks):
    settings.N8N_WEBHOOK_BASE_URL = ""
    settings.N8N_BASE_URL = ""
    organization, owner = make_org(django_user_model, "off")
    response = client_for(owner, organization).post(
        "/api/v1/jobs/",
        {"idempotency_key": "off-1", "task_type": "SCHEDULED_AUTOMATION", "input_payload": {"action": "x"}},
        format="json",
    )
    assert response.status_code == 400  # n8n task types are only offered when n8n is configured


# Event-triggered workflows ----------------------------------------------------------


def test_domain_events_fan_out_to_subscribed_workflows_and_complete(
    n8n, django_user_model, django_capture_on_commit_callbacks
):
    organization, owner = make_org(django_user_model, "events")
    with django_capture_on_commit_callbacks(execute=True):
        event = enqueue_outbox_event(
            "jobs.job.failed", "k", {"job_id": "j1", "organization_id": str(organization.id), "error": "boom"}
        )
    delivery = WorkflowEventDelivery.objects.get(event_id=event.id)
    assert delivery.definition.key == "event-notifications"
    assert delivery.status == WorkflowEventDelivery.Status.ACCEPTED
    payload = n8n.requests[-1]["payload"]
    assert payload["eventType"] == "jobs.job.failed"
    assert payload["context"]["recipients"] == [owner.email]
    assert payload["callbacks"]["status"] == f"{API}/n8n/deliveries/{delivery.id}/status/"

    done = signed(
        f"n8n/deliveries/{delivery.id}/status/", {"status": "completed", "summary": {"notified": True}}
    )
    assert done.status_code == 200
    delivery.refresh_from_db()
    assert delivery.status == WorkflowEventDelivery.Status.COMPLETED
    assert OutboxEvent.objects.filter(topic__endswith="orchestration.delivery.completed").exists()


def test_event_deliveries_retry_then_fail(n8n, django_user_model, django_capture_on_commit_callbacks):
    n8n.script("event-notifications", *[(502, {})] * 8)
    with django_capture_on_commit_callbacks(execute=True):
        event = enqueue_outbox_event("events.dead_lettered", "k", {"consumer_group": "g", "error": "x"})
    delivery = WorkflowEventDelivery.objects.get(event_id=event.id)
    for _ in range(delivery.definition.max_attempts):
        WorkflowEventDelivery.objects.filter(id=delivery.id).update(next_attempt_at=timezone.now())
        deliveries.sweep_deliveries()
    delivery.refresh_from_db()
    assert delivery.status == WorkflowEventDelivery.Status.FAILED
    assert delivery.attempts == delivery.definition.max_attempts


def test_unsubscribed_events_create_no_deliveries(n8n):
    event = enqueue_outbox_event("asset.created", "k", {"asset_id": "a"})
    assert not WorkflowEventDelivery.objects.filter(event_id=event.id).exists()


# Integrations and knowledge sync ------------------------------------------------------


def test_integrations_contract_connect_test_and_disconnect(n8n, django_user_model):
    organization, owner = make_org(django_user_model, "integrations")
    api = client_for(owner, organization)
    bad = api.post(
        "/api/v1/integrations/connect/",
        {"key": "google_drive", "config": {"folderId": "../x"}},
        format="json",
    )
    assert bad.status_code == 400
    n8n.script("integration-test", (200, {"ok": True, "message": "Google Drive connection is healthy."}))
    connected = api.post(
        "/api/v1/integrations/connect/",
        {"key": "google_drive", "displayName": "Docs", "config": {"folderId": "1AbCdEfGhIjKlMnOp"}},
        format="json",
    )
    assert connected.status_code == 201, connected.content
    integration = connected.json()
    assert {k: integration[k] for k in ("key", "name", "displayName", "connected", "status")} == {
        "key": "google_drive",
        "name": "Google Drive",
        "displayName": "Docs",
        "connected": True,
        "status": "connected",
    }
    assert n8n.requests[-1]["payload"]["integration"]["config"] == {"folderId": "1AbCdEfGhIjKlMnOp"}
    assert [item["id"] for item in api.get("/api/v1/integrations/").json()] == [integration["id"]]

    n8n.script("integration-test", (200, {"ok": False, "message": "Folder not shared."}))
    check = api.post(f"/api/v1/integrations/{integration['id']}/test/")
    assert check.json() == {"ok": False, "message": "Folder not shared."}
    assert api.get("/api/v1/integrations/").json()[0]["status"] == "error"

    assert api.delete(f"/api/v1/integrations/{integration['id']}/").status_code == 204
    assert api.get("/api/v1/integrations/").json()[0]["status"] == "disconnected"


def test_knowledge_integration_sync_round_trip(n8n, django_user_model, django_capture_on_commit_callbacks):
    from apps.integrations.facade import connector_for
    from apps.integrations.models import ConnectorAccount
    from apps.knowledge.models import Collection, Document, Source, SyncRun
    from apps.knowledge.tasks import sync_source

    organization, owner = make_org(django_user_model, "ksync")
    account = ConnectorAccount.objects.create(
        organization=organization,
        connector=connector_for("github"),
        user=owner,
        name="Docs repo",
        status=ConnectorAccount.Status.ACTIVE,
        metadata={"config": {"owner": "acme", "repo": "docs"}},
    )
    collection = Collection.objects.create(
        organization=organization,
        name="Repo",
        embedding_provider="echo",
        embedding_model="echo-deterministic",
        created_by=owner,
    )
    source = Source.objects.create(
        collection=collection,
        source_type="integration",
        name="acme/docs",
        config={"integrationId": str(account.id)},
        created_by=owner,
    )

    def sync(documents):
        with django_capture_on_commit_callbacks(execute=True):
            sync_source(str(source.id))
        delivery = WorkflowEventDelivery.objects.filter(
            event_type="knowledge.integration.sync_requested"
        ).latest("created_at")
        sent = n8n.requests[-1]["payload"]
        assert sent["data"]["integration"] == {
            "id": str(account.id),
            "key": "github",
            "config": {"owner": "acme", "repo": "docs"},
        }
        assert sent["callbacks"]["documents"] == f"{API}/n8n/knowledge/sources/{source.id}/documents/"
        with django_capture_on_commit_callbacks(execute=True):
            pushed = signed(
                f"n8n/knowledge/sources/{source.id}/documents/",
                {"deliveryId": str(delivery.id), "documents": documents, "final": True},
            )
            assert pushed.status_code == 200, pushed.content
            done = signed(f"n8n/deliveries/{delivery.id}/status/", {"status": "completed"})
            assert done.status_code == 200
        return SyncRun.objects.filter(source=source).latest("started_at")

    docs = [
        {
            "externalId": "github:acme/docs:README.md",
            "title": "README.md",
            "text": "# Install\nRun the installer.",
        },
        {"externalId": "github:acme/docs:faq.md", "title": "faq.md", "text": "# FAQ\nAsk us anything."},
    ]
    run = sync(docs)
    assert run.status == SyncRun.Status.COMPLETED and run.documents_added == 2
    indexed = Document.objects.filter(source=source, status=Document.Status.INDEXED)
    assert indexed.count() == 2 and all(d.chunk_count > 0 for d in indexed)
    assert Source.objects.get(id=source.id).status == Source.Status.INDEXED
    assert ConnectorAccount.objects.get(id=account.id).last_sync_at is not None

    run = sync(docs[:1])  # faq.md was removed upstream
    assert run.documents_deleted == 1
    assert Document.objects.get(external_id=docs[1]["externalId"]).status == Document.Status.DELETED

    # A push without an open delivery for the source is refused.
    stale = signed(
        f"n8n/knowledge/sources/{source.id}/documents/",
        {"deliveryId": str(WorkflowEventDelivery.objects.latest("created_at").id), "documents": []},
    )
    assert stale.status_code == 409


# Automations ------------------------------------------------------------------------


def test_automations_validate_inputs_schedule_and_run_through_n8n(
    n8n, django_user_model, django_capture_on_commit_callbacks
):
    from apps.orchestration.automations import run_due_automations

    organization, owner = make_org(django_user_model, "automation")
    api = client_for(owner, organization)
    spam = api.post(
        "/api/v1/automations/",
        {
            "name": "Spam",
            "schedule": "0 9 * * 1",
            "input": {"action": "email", "to": ["stranger@else.test"], "subject": "Hi", "text": "x"},
        },
        format="json",
    )
    assert spam.status_code == 400  # recipients must be organization members
    every_minute = api.post(
        "/api/v1/automations/",
        {
            "name": "x",
            "schedule": "* * * * *",
            "input": {"action": "slack_message", "channel": "C0123456", "text": "hi"},
        },
        format="json",
    )
    assert every_minute.status_code == 400
    created = api.post(
        "/api/v1/automations/",
        {
            "name": "Weekly digest",
            "schedule": "0 9 * * 1",
            "input": {"action": "email", "to": [owner.email], "subject": "Digest", "text": "Weekly digest"},
        },
        format="json",
    )
    assert created.status_code == 201, created.content
    automation = Automation.objects.get(id=created.json()["id"])
    assert automation.next_run_at > timezone.now()

    Automation.objects.filter(id=automation.id).update(next_run_at=timezone.now() - timedelta(minutes=1))
    with django_capture_on_commit_callbacks(execute=True):
        assert run_due_automations() == 1
    automation.refresh_from_db()
    assert automation.run_count == 1 and automation.next_run_at > timezone.now()
    job = automation.last_job
    assert job.task_type == Job.TaskType.SCHEDULED_AUTOMATION
    assert n8n.requests[-1]["payload"]["input"]["automationId"] == str(automation.id)
    assert WorkflowRun.objects.get(job=job).status == WorkflowRun.Status.RUNNING

    with django_capture_on_commit_callbacks(execute=True):
        manual = api.post(f"/api/v1/automations/{automation.id}/run/")
    assert manual.status_code == 201


# Deploying definitions to n8n -----------------------------------------------------------


def test_push_deploys_links_the_error_workflow_and_check_detects_drift(n8n):
    call_command("n8n_workflows", "push")
    deployed = {w["name"]: w for w in n8n.workflows.values()}
    assert len(deployed) == WorkflowDefinition.objects.count() == 6  # every version is stored in n8n
    assert deployed["jt-code.event-notifications.v1"]["active"] is False  # superseded by v2
    assert deployed["jt-code.event-notifications.v2"]["active"] is True
    error_id = deployed["jt-code.error-handler.v1"]["id"]
    automation = deployed["jt-code.scheduled-automation.v1"]
    assert automation["active"] is True
    assert automation["settings"]["errorWorkflow"] == error_id
    slack = next(n for n in automation["nodes"] if n.get("credentials", {}).get("slackApi"))
    assert slack["credentials"]["slackApi"]["id"] == "c-slack"
    assert deployed["jt-code.error-handler.v1"]["active"] is False  # triggered by n8n, not activated
    assert all(d.n8n_workflow_id for d in WorkflowDefinition.objects.filter(is_active=True))

    call_command("n8n_workflows", "check")
    automation["nodes"][1]["parameters"]["jsCode"] = "return [];"  # someone edited production in the UI
    with pytest.raises(CommandError, match="differs"):
        call_command("n8n_workflows", "check")


def test_push_reports_missing_credentials_and_keeps_those_workflows_inactive(n8n, settings):
    settings.N8N_CREDENTIAL_IDS = {}
    with pytest.raises(CommandError, match="N8N_CREDENTIAL_SLACK"):
        call_command("n8n_workflows", "push")
    assert all(not w["active"] for w in n8n.workflows.values())
