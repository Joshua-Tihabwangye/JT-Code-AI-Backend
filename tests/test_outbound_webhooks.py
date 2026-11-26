"""Outbound webhooks (P16-2): delivered, signed, tenant-safe, retried, and managed by admins only."""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
from datetime import timedelta

import httpx
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.conversations.models import ChatRequest, Conversation
from apps.events.outbox import add_outbox_event
from apps.identity.models import Organization, Role, User, UserOrganization, UserRole
from apps.integrations import outbound
from apps.integrations.models import Webhook, WebhookDelivery
from apps.tools import egress

PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """hooks.example.com is public; internal.example.com points into the private network."""

    def getaddrinfo(host, port, *args, **kwargs):
        address = {"hooks.example.com": PUBLIC_IP, "internal.example.com": "10.0.0.5"}.get(host)
        if address is None:
            raise socket.gaierror(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(egress.socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def sent(monkeypatch):
    """Records each delivery request; answers with ``sent.status`` (default 200)."""

    class Sender:
        status = 200
        requests: list[dict] = []

        def __call__(self, method, url, *, headers=None, content=None, max_redirects=3, **kwargs):
            self.requests.append(
                {"url": url, "headers": headers, "body": content, "max_redirects": max_redirects}
            )
            return egress.EgressResponse(
                self.status, httpx.Headers({"content-type": "text/plain"}), b"ok", url
            )

    sender = Sender()
    sender.requests = []
    monkeypatch.setattr(egress, "safe_request", sender)
    return sender


@pytest.fixture
def owner():
    return User.objects.create(
        username="hook-owner", supabase_user_id="sb-hook-owner", email="owner@example.com"
    )


@pytest.fixture
def org(owner):
    organization = Organization.objects.create(name="Hook workspace", owner=owner)
    UserOrganization.objects.create(user=owner, organization=organization)
    return organization


def _admin(user, org) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    client.credentials(HTTP_X_ORGANIZATION_ID=str(org.id))
    return client


def _webhook(org, **fields) -> Webhook:
    defaults = {"name": "Hook", "url": "https://hooks.example.com/jt", "secret": "whsec_test"}
    return Webhook.objects.create(organization=org, **{**defaults, **fields})


def _chat_request(owner, org, key="1") -> ChatRequest:
    conversation = Conversation.objects.create(owner=owner, organization=org, title="c")
    return ChatRequest.objects.create(
        owner=owner,
        organization=org,
        conversation=conversation,
        idempotency_key=f"hook-{key}",
        request_fingerprint=key * 64,
        input_text="hi",
        trace_id=f"trace-{key}",
    )


# --- Fan-out: which events reach which endpoints ----------------------------------------------------


@pytest.mark.django_db
def test_a_catalogue_event_reaches_only_its_own_workspace(owner, org):
    other_owner = User.objects.create(username="other", supabase_user_id="sb-other", email="o@example.com")
    other = Organization.objects.create(name="Other", owner=other_owner)
    mine, theirs = _webhook(org), _webhook(other)
    request = _chat_request(owner, org)

    event = add_outbox_event(
        "chat.request.completed",
        str(request.id),
        # A payload naming another workspace is ignored: the organization comes from the entity.
        {"requestId": str(request.id), "organization_id": str(other.id), "traceId": "internal"},
    )

    deliveries = WebhookDelivery.objects.all()
    assert [d.webhook_id for d in deliveries] == [mine.id]
    payload = deliveries[0].payload
    assert payload["id"] == str(event.id) and payload["type"] == "chat.request.completed"
    assert payload["organization_id"] == str(org.id)
    assert "traceId" not in payload["data"]  # internal keys never leave the platform
    assert not WebhookDelivery.objects.filter(webhook=theirs).exists()


@pytest.mark.django_db
def test_events_outside_the_catalogue_or_without_a_known_entity_are_not_delivered(owner, org):
    _webhook(org)
    add_outbox_event("billing.stripe.webhook", "evt_1", {"organization_id": str(org.id)})
    add_outbox_event("chat.request.completed", "00000000-0000-0000-0000-000000000000", {})
    add_outbox_event("chat.request.completed", "not-a-uuid", {})
    assert not WebhookDelivery.objects.exists()


@pytest.mark.django_db
def test_custom_filters_paused_and_disabled_endpoints(owner, org):
    wanted = _webhook(org, event_filter=Webhook.EventFilter.CUSTOM, events=["chat.request.completed"])
    _webhook(org, event_filter=Webhook.EventFilter.CUSTOM, events=["jobs.job.completed"])
    _webhook(org, status=Webhook.Status.PAUSED)
    _webhook(org, status=Webhook.Status.DISABLED)
    request = _chat_request(owner, org)
    add_outbox_event("chat.request.completed", str(request.id), {})
    assert list(WebhookDelivery.objects.values_list("webhook_id", flat=True)) == [wanted.id]


# --- Delivery: signed, SSRF-safe, retried, disabled ---------------------------------------------------


@pytest.mark.django_db
def test_a_delivery_is_signed_and_marks_the_endpoint_verified(owner, org, sent):
    webhook = _webhook(org, headers={"Authorization": "Bearer customer-token", "X-JT-Event": "spoof"})
    request = _chat_request(owner, org)
    add_outbox_event("chat.request.completed", str(request.id), {"status": "completed"})
    delivery = WebhookDelivery.objects.get()

    assert outbound.deliver(str(delivery.id)) == WebhookDelivery.Status.DELIVERED
    (call,) = sent.requests
    assert call["max_redirects"] == 0
    headers = call["headers"]
    assert headers["X-JT-Event"] == "chat.request.completed"  # the customer header cannot override ours
    assert headers["X-JT-Delivery"] == str(delivery.id)
    assert headers["Authorization"] == "Bearer customer-token"
    timestamp, signature = (part.split("=", 1)[1] for part in headers["X-JT-Signature"].split(","))
    expected = hmac.new(b"whsec_test", timestamp.encode() + b"." + call["body"], hashlib.sha256).hexdigest()
    assert hmac.compare_digest(signature, expected)
    assert json.loads(call["body"])["data"] == {"status": "completed"}

    webhook.refresh_from_db()
    assert webhook.is_verified and webhook.last_success_at and webhook.failure_count == 0
    # Delivered once: a duplicate task does not send again.
    assert outbound.deliver(str(delivery.id)) == WebhookDelivery.Status.DELIVERED
    assert len(sent.requests) == 1


@pytest.mark.django_db
def test_failures_retry_with_backoff_then_fail_and_disable_the_endpoint(owner, org, sent, settings):
    settings.WEBHOOK_MAX_ATTEMPTS = 2
    settings.WEBHOOK_DISABLE_AFTER_FAILURES = 1
    webhook = _webhook(org)
    request = _chat_request(owner, org)
    add_outbox_event("chat.request.completed", str(request.id), {})
    delivery = WebhookDelivery.objects.get()
    sent.status = 500

    assert outbound.deliver(str(delivery.id)) == WebhookDelivery.Status.RETRYING
    delivery.refresh_from_db()
    assert delivery.error_message == "The endpoint answered HTTP 500."
    assert delivery.next_retry_at > timezone.now() + timedelta(seconds=20)
    # Not due yet: a second task (or the sweep) does not send early.
    assert outbound.deliver(str(delivery.id)) == "not-due"
    assert str(delivery.id) not in outbound.due_delivery_ids()

    WebhookDelivery.objects.filter(id=delivery.id).update(next_retry_at=timezone.now())
    assert str(delivery.id) in outbound.due_delivery_ids()
    assert outbound.deliver(str(delivery.id)) == WebhookDelivery.Status.FAILED
    webhook.refresh_from_db()
    assert webhook.failure_count == 1 and webhook.status == Webhook.Status.DISABLED
    assert len(sent.requests) == 2


@pytest.mark.django_db
def test_a_private_destination_is_refused_at_send_time(owner, org):
    # DNS changed after the endpoint was saved (rebinding): the egress layer refuses it when sending.
    webhook = _webhook(org, url="https://internal.example.com/hook")
    delivery = outbound.create_test_delivery(webhook)
    assert outbound.deliver(str(delivery.id)) == WebhookDelivery.Status.FAILED
    delivery.refresh_from_db()
    assert delivery.error_message.startswith("Refused:") and "non-public" in delivery.error_message


@pytest.mark.django_db
def test_a_held_lease_blocks_a_concurrent_attempt(owner, org, sent):
    webhook = _webhook(org)
    delivery = outbound.create_test_delivery(webhook)
    WebhookDelivery.objects.filter(id=delivery.id).update(
        next_retry_at=timezone.now() + timedelta(seconds=60)
    )
    assert outbound.deliver(str(delivery.id)) == "not-due"
    assert sent.requests == []


# --- API: admins only; the secret shown once; validation ------------------------------------------------


@pytest.mark.django_db
def test_create_returns_the_secret_once_and_test_sends_a_real_event(
    owner, org, sent, django_capture_on_commit_callbacks
):
    client = _admin(owner, org)
    created = client.post(
        "/api/v1/webhooks/",
        {"name": "Ops", "url": "https://hooks.example.com/jt", "events": ["chat.request.completed"]},
        format="json",
    )
    assert created.status_code == 201, created.content
    body = created.json()
    assert body["secret"].startswith("whsec_") and body["event_filter"] == "custom"
    listed = client.get("/api/v1/webhooks/").json()
    assert body["secret"] not in json.dumps(listed)

    with django_capture_on_commit_callbacks(execute=True):
        tested = client.post(f"/api/v1/webhooks/{body['id']}/test/")
    assert tested.status_code == 202
    (call,) = sent.requests
    assert json.loads(call["body"])["type"] == "webhook.test"
    deliveries = client.get(f"/api/v1/webhooks/{body['id']}/deliveries/").json()
    rows = deliveries["results"] if isinstance(deliveries, dict) else deliveries
    assert rows[0]["status"] == "delivered"

    rotated = client.post(f"/api/v1/webhooks/{body['id']}/rotate-secret/").json()["secret"]
    assert rotated != body["secret"] and Webhook.objects.get(id=body["id"]).secret == rotated


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change",
    [
        {"url": "http://hooks.example.com/jt"},
        {"url": "https://internal.example.com/jt"},
        {"url": "https://hooks.example.com:8443/jt"},
        {"url": f"https://{PUBLIC_IP}/jt"},
        {"events": ["billing.stripe.webhook"]},
        {"headers": {"Host": "evil"}},
        {"headers": {"X-JT-Signature": "forged"}},
        {"headers": {"X-Ok": "line\r\nInjected: 1"}},
        {"event_filter": "custom", "events": []},
    ],
)
def test_unsafe_urls_unknown_events_and_bad_headers_are_refused(owner, org, change):
    body = {"name": "Bad", "url": "https://hooks.example.com/jt", **change}
    response = _admin(owner, org).post("/api/v1/webhooks/", body, format="json")
    assert response.status_code == 400, (change, response.content)
    assert not Webhook.objects.exists()


@pytest.mark.django_db
def test_only_admins_manage_webhooks_and_the_platform_status_is_not_writable(owner, org):
    editor = User.objects.create(
        username="hook-editor", supabase_user_id="sb-hook-editor", email="e@example.com"
    )
    UserOrganization.objects.create(user=editor, organization=org)
    role, _ = Role.objects.get_or_create(name=Role.RoleType.EDITOR)
    UserRole.objects.create(user=editor, organization=org, role=role)
    webhook = _webhook(org, status=Webhook.Status.DISABLED, failure_count=20)

    as_editor = _admin(editor, org)
    assert as_editor.get("/api/v1/webhooks/").status_code == 403
    assert (
        as_editor.post("/api/v1/webhooks/", {"name": "x", "url": "https://hooks.example.com/x"}).status_code
        == 403
    )
    assert as_editor.get("/api/v1/webhook-deliveries/").status_code == 403

    as_owner = _admin(owner, org)
    assert (
        as_owner.patch(f"/api/v1/webhooks/{webhook.id}/", {"status": "error"}, format="json").status_code
        == 400
    )
    resumed = as_owner.patch(f"/api/v1/webhooks/{webhook.id}/", {"status": "active"}, format="json")
    assert resumed.status_code == 200, resumed.content
    webhook.refresh_from_db()
    assert webhook.status == Webhook.Status.ACTIVE and webhook.failure_count == 0
    assert "secret" not in resumed.json()
