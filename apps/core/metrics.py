"""Prometheus metrics (Phase 15).

Metric names are part of the operational contract: the Grafana dashboards and
alert rules under ``infra/`` reference them, and ``tests/test_phase15_*``
fails if a dashboard queries a metric this module does not export.

HTTP metrics are labelled by the URL *route template* (never the raw path), so
cardinality is bounded. Business state (outbox backlog, queued jobs, open
reservations, failed Stripe events, ...) is read from PostgreSQL at scrape time
by :class:`DatabaseStateCollector` and cached briefly.

With several worker processes (gunicorn, Celery prefork) set
``PROMETHEUS_MULTIPROC_DIR`` to a per-container writable directory; the
exposition then aggregates every process.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import Callable, Iterable
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

_LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
_LONG_BUCKETS = (0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)

HTTP_REQUESTS = Counter(
    "jt_http_requests_total", "HTTP requests by route template and status.", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "jt_http_request_duration_seconds",
    "HTTP request latency by route template.",
    ["method", "route"],
    buckets=_LATENCY_BUCKETS,
)
HTTP_IN_PROGRESS = Gauge(
    "jt_http_requests_in_progress", "HTTP requests being served.", multiprocess_mode="livesum"
)
CELERY_TASKS = Counter("jt_celery_tasks_total", "Celery task outcomes.", ["task", "state"])
CELERY_TASK_DURATION = Histogram(
    "jt_celery_task_duration_seconds", "Celery task run time.", ["task"], buckets=_LONG_BUCKETS
)
AI_REQUESTS = Counter("jt_ai_requests_total", "AI gateway model runs.", ["provider", "status"])
AI_TOKENS = Counter("jt_ai_tokens_total", "Model tokens.", ["provider", "direction"])
AI_COST = Counter("jt_ai_provider_cost_usd_total", "Provider cost in USD.", ["provider"])
AI_LATENCY = Histogram(
    "jt_ai_request_duration_seconds", "Model run latency.", ["provider"], buckets=_LONG_BUCKETS
)
CREDITS_CHARGED = Counter("jt_usage_credits_charged_total", "Credits charged to tenants.", ["feature"])
RATE_LIMITED = Counter("jt_rate_limited_total", "Requests rejected by rate limits.", ["dimension", "scope"])
WEBHOOKS = Counter("jt_webhooks_received_total", "Inbound webhooks by outcome.", ["source", "outcome"])
SECURITY_EVENTS = Counter("jt_security_events_total", "Security-relevant rejections.", ["kind"])
AUDIT_EVENTS = Counter("jt_audit_events_total", "Audit events recorded.", ["category", "severity"])
OUTBOX_PUBLISHED = Counter("jt_outbox_published_total", "Outbox events published to Kafka.", ["outcome"])
COST_ANOMALIES = Counter("jt_cost_anomalies_total", "Hourly provider-cost anomalies.", ["scope"])
WORKFLOW_DISPATCHES = Counter(
    "jt_workflow_dispatches_total", "n8n workflow dispatch attempts.", ["workflow", "outcome"]
)
WORKFLOW_CALLBACKS = Counter(
    "jt_workflow_callbacks_total", "n8n callbacks by kind and outcome.", ["kind", "outcome"]
)

_EXCLUDED_PATHS = ("/metrics",)


_NAMED_GROUP = re.compile(r"\(\?P<(\w+)>[^)]*\)")


def route_template(match: Any) -> str:
    """The matched URL pattern without regex anchors (``api/v1/jobs/<uuid:job_id>/``)."""
    if match is None:
        return ""
    route = _NAMED_GROUP.sub(r"<\1>", str(match.route or ""))
    return route.replace("^", "").replace("$", "").replace("\\", "")


def route_label(request: HttpRequest) -> str:
    match = getattr(request, "resolver_match", None)
    if match is None:
        return "unmatched"
    return "/" + (route_template(match) or match.view_name or "unknown")


class MetricsMiddleware:
    """Record RED metrics per route template (placed directly after request context)."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path.startswith(_EXCLUDED_PATHS):
            return self.get_response(request)
        started = time.perf_counter()
        HTTP_IN_PROGRESS.inc()
        status = 500
        try:
            response = self.get_response(request)
            status = response.status_code
            return response
        finally:
            HTTP_IN_PROGRESS.dec()
            route = route_label(request)
            HTTP_REQUESTS.labels(request.method or "", route, str(status)).inc()
            HTTP_LATENCY.labels(request.method or "", route).observe(time.perf_counter() - started)


# Business-state gauges read from PostgreSQL ---------------------------------

_STATE_CACHE: dict[str, Any] = {"at": 0.0, "families": []}


def _state_queries() -> Iterable[
    tuple[str, str, list[str], Callable[[], Iterable[tuple[tuple[str, ...], float]]]]
]:
    from django.db.models import Count, Q, Sum

    from apps.billing.models import StripeEvent
    from apps.conversations.models import ChatRequest
    from apps.events.models import DeadLetterEvent, OutboxEvent
    from apps.jobs.models import Job, WorkflowRun
    from apps.usage.models import UsageReservation

    def outbox() -> Iterable[tuple[tuple[str, ...], float]]:
        rows = OutboxEvent.objects.exclude(status=OutboxEvent.Status.PUBLISHED).aggregate(
            pending=Count("id", filter=~Q(status=OutboxEvent.Status.FAILED)),
            failed=Count("id", filter=Q(status=OutboxEvent.Status.FAILED)),
        )
        return [(("pending",), rows["pending"]), (("failed",), rows["failed"])]

    def jobs() -> Iterable[tuple[tuple[str, ...], float]]:
        active = (Job.Status.QUEUED, Job.Status.VALIDATING, Job.Status.RUNNING, Job.Status.WAITING_APPROVAL)
        counts = dict(
            Job.objects.filter(status__in=active).values_list("status").annotate(n=Count("id")).order_by()
        )
        return [((status,), counts.get(status, 0)) for status in active]

    def chat() -> Iterable[tuple[tuple[str, ...], float]]:
        counts = dict(
            ChatRequest.objects.filter(status__in=("queued", "running"))
            .values_list("status")
            .annotate(n=Count("id"))
            .order_by()
        )
        return [((status,), counts.get(status, 0)) for status in ("queued", "running")]

    def reservations() -> Iterable[tuple[tuple[str, ...], float]]:
        rows = UsageReservation.objects.filter(status=UsageReservation.Status.HELD).aggregate(
            n=Count("id"), credits=Sum("credits_reserved")
        )
        return [(("count",), rows["n"] or 0), (("credits",), float(rows["credits"] or 0))]

    def stripe() -> Iterable[tuple[tuple[str, ...], float]]:
        counts = dict(
            StripeEvent.objects.exclude(status__in=(StripeEvent.Status.PROCESSED, StripeEvent.Status.IGNORED))
            .values_list("status")
            .annotate(n=Count("id"))
            .order_by()
        )
        return [((str(status),), count) for status, count in counts.items()] or [(("failed",), 0)]

    def dead_letters() -> Iterable[tuple[tuple[str, ...], float]]:
        return [((), DeadLetterEvent.objects.filter(replayed_at__isnull=True).count())]

    def workflows() -> Iterable[tuple[tuple[str, ...], float]]:
        active = (WorkflowRun.Status.PENDING, WorkflowRun.Status.RUNNING)
        counts = dict(
            WorkflowRun.objects.filter(status__in=active, max_attempts__gt=0)
            .values_list("status")
            .annotate(n=Count("id"))
            .order_by()
        )
        return [((status,), counts.get(status, 0)) for status in active]

    return (
        ("jt_outbox_events", "Unpublished outbox events.", ["state"], outbox),
        ("jt_jobs_active", "Jobs not yet terminal.", ["status"], jobs),
        ("jt_chat_requests_active", "Chat requests not yet terminal.", ["status"], chat),
        ("jt_usage_open_reservations", "Open credit reservations.", ["measure"], reservations),
        ("jt_stripe_events_unprocessed", "Stripe events not processed.", ["status"], stripe),
        ("jt_dead_letter_events", "Dead-lettered events awaiting replay.", [], dead_letters),
        ("jt_workflow_runs_active", "n8n workflow runs not yet terminal.", ["status"], workflows),
    )


class DatabaseStateCollector(Collector):
    """Expose business-state gauges; failures surface as ``jt_metrics_collector_up 0``."""

    def collect(self) -> Iterable[Metric]:
        ttl = float(getattr(settings, "METRICS_STATE_CACHE_SECONDS", 15))
        now = time.monotonic()
        if _STATE_CACHE["families"] and now - _STATE_CACHE["at"] < ttl:
            yield from _STATE_CACHE["families"]
            return
        families: list[Metric] = []
        healthy = 1.0
        for name, documentation, labels, query in _state_queries():
            family = GaugeMetricFamily(name, documentation, labels=labels)
            try:
                for values, value in query():
                    family.add_metric(list(values), float(value))
            except Exception:  # noqa: BLE001 - a broken query must not break the whole scrape
                healthy = 0.0
                continue
            families.append(family)
        up = GaugeMetricFamily("jt_metrics_collector_up", "1 when every state query succeeded.")
        up.add_metric([], healthy)
        families.append(up)
        _STATE_CACHE.update(at=now, families=families)
        yield from families


_STATE_COLLECTOR = DatabaseStateCollector()
_registered = False


def _registry() -> CollectorRegistry:
    global _registered
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
        if getattr(settings, "METRICS_DATABASE_STATE", True):
            registry.register(_STATE_COLLECTOR)
        return registry
    if not _registered and getattr(settings, "METRICS_DATABASE_STATE", True):
        REGISTRY.register(_STATE_COLLECTOR)
        _registered = True
    return REGISTRY


def render_metrics() -> bytes:
    return generate_latest(_registry())


def metrics_view(request: HttpRequest) -> HttpResponse:
    """Prometheus exposition, protected by ``Authorization: Bearer <METRICS_AUTH_TOKEN>``.

    Without a token the endpoint is only served when ``DEBUG`` is on; deployable
    profiles must configure ``METRICS_AUTH_TOKEN`` (settings validation).
    """
    token = getattr(settings, "METRICS_AUTH_TOKEN", "")
    if token:
        scheme, _, supplied = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(supplied.strip(), token):
            SECURITY_EVENTS.labels("metrics_unauthorized").inc()
            return HttpResponse("Unauthorized\n", status=401, content_type="text/plain")
    elif not settings.DEBUG:
        return HttpResponse(status=404)
    if request.method != "GET":
        return HttpResponse(status=405)
    response = HttpResponse(render_metrics(), content_type=CONTENT_TYPE_LATEST)
    response["Cache-Control"] = "no-store"
    return response


# Celery signal hooks ---------------------------------------------------------

_task_started: dict[str, float] = {}


def _task_name(sender: Any, task: Any = None) -> str:
    target = task or sender
    return str(getattr(target, "name", None) or target or "unknown")


def connect_celery_signals() -> None:
    from celery import signals

    def prerun(task_id: str | None = None, task: Any = None, **_: Any) -> None:
        if task_id:
            _task_started[task_id] = time.perf_counter()

    def postrun(task_id: str | None = None, task: Any = None, state: str | None = None, **_: Any) -> None:
        name = _task_name(None, task)
        started = _task_started.pop(task_id or "", None)
        if started is not None:
            CELERY_TASK_DURATION.labels(name).observe(time.perf_counter() - started)
        if state == "SUCCESS":
            CELERY_TASKS.labels(name, "succeeded").inc()

    def failure(sender: Any = None, **_: Any) -> None:
        CELERY_TASKS.labels(_task_name(sender), "failed").inc()

    def retry(sender: Any = None, **_: Any) -> None:
        CELERY_TASKS.labels(_task_name(sender), "retried").inc()

    signals.task_prerun.connect(prerun, weak=False, dispatch_uid="jt-metrics-prerun")
    signals.task_postrun.connect(postrun, weak=False, dispatch_uid="jt-metrics-postrun")
    signals.task_failure.connect(failure, weak=False, dispatch_uid="jt-metrics-failure")
    signals.task_retry.connect(retry, weak=False, dispatch_uid="jt-metrics-retry")

    def start_exporter(**_: Any) -> None:
        port = int(getattr(settings, "CELERY_METRICS_PORT", 0) or 0)
        if port:
            from prometheus_client import start_http_server

            start_http_server(port, registry=_registry())

    signals.worker_ready.connect(start_exporter, weak=False, dispatch_uid="jt-metrics-exporter")


def connect_model_signals() -> None:
    from django.db.models.signals import post_save

    from apps.ai_gateway.models import ModelRun
    from apps.usage.models import UsageRecord

    terminal = {ModelRun.Status.COMPLETED, ModelRun.Status.FAILED, ModelRun.Status.TIMEOUT}

    def model_run_saved(sender: Any, instance: ModelRun, created: bool, **_: Any) -> None:
        if instance.status not in terminal or getattr(instance, "_jt_metrics_recorded", False):
            return
        instance._jt_metrics_recorded = True  # type: ignore[attr-defined]
        provider = str(getattr(instance.provider, "type", "") or "unknown")
        AI_REQUESTS.labels(provider, str(instance.status)).inc()
        AI_TOKENS.labels(provider, "input").inc(instance.input_tokens or 0)
        AI_TOKENS.labels(provider, "output").inc(instance.output_tokens or 0)
        AI_COST.labels(provider).inc(float(instance.provider_cost_usd or 0))
        if instance.latency_ms:
            AI_LATENCY.labels(provider).observe(instance.latency_ms / 1000)

    def usage_recorded(sender: Any, instance: UsageRecord, created: bool, **_: Any) -> None:
        if created:
            CREDITS_CHARGED.labels(str(instance.feature)).inc(float(instance.credits_charged or 0))

    post_save.connect(model_run_saved, sender=ModelRun, weak=False, dispatch_uid="jt-metrics-model-run")
    post_save.connect(usage_recorded, sender=UsageRecord, weak=False, dispatch_uid="jt-metrics-usage")
