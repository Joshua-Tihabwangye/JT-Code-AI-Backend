"""OpenTelemetry distributed tracing (Phase 15).

Tracing is enabled when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set: spans are
batched to an OTLP/HTTP collector (``infra/otel/collector.yaml``). Django,
Celery, psycopg, Redis and httpx are instrumented automatically; W3C trace
context crosses the asynchronous boundaries this codebase owns:

* **Kafka** - the outbox stores ``traceparent`` with each event (captured in
  the producing request), the publisher forwards it as a Kafka header and
  ``run_kafka_consumer`` continues the trace in a CONSUMER span.
* **n8n** - every Django->n8n request carries ``traceparent``; n8n callbacks
  that echo it are joined to the same trace.

Without an endpoint the OpenTelemetry API is a no-op, so the helpers below are
always safe to call.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from django.conf import settings
from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.trace import Span, SpanKind

logger = logging.getLogger(__name__)
TRACER_NAME = "jt-code"
_configured = False


def _parse_headers(value: str) -> dict[str, str]:
    headers = {}
    for item in value.split(","):
        key, sep, val = item.partition("=")
        if sep and key.strip():
            headers[key.strip()] = val.strip()
    return headers


def configure_tracing(*, force: bool = False) -> bool:
    """Install the SDK tracer provider and library instrumentation once per process."""
    global _configured
    endpoint = getattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if _configured or (not endpoint and not force):
        return _configured
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    resource = Resource.create(
        {
            "service.name": settings.OTEL_SERVICE_NAME,
            "service.version": settings.OTEL_SERVICE_VERSION,
            "deployment.environment": settings.OTEL_ENVIRONMENT,
        }
    )
    provider = TracerProvider(
        resource=resource, sampler=ParentBased(TraceIdRatioBased(settings.OTEL_TRACES_SAMPLE_RATIO))
    )
    if endpoint:
        exporter = OTLPSpanExporter(
            endpoint=endpoint.rstrip("/") + "/v1/traces",
            headers=_parse_headers(settings.OTEL_EXPORTER_OTLP_HEADERS),
            timeout=10,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    instrument_libraries()
    _configured = True
    return True


def instrument_libraries() -> None:
    from opentelemetry.instrumentation.celery import CeleryInstrumentor
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    # Health probes and the metrics scrape would otherwise dominate traces.
    DjangoInstrumentor().instrument(excluded_urls="api/v1/health/.*,metrics")
    CeleryInstrumentor().instrument()  # type: ignore[no-untyped-call]
    # SQL text can contain literals; statement capture is limited to the
    # parameterised query that psycopg sends.
    PsycopgInstrumentor().instrument(enable_commenter=False)
    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


def inject_headers(carrier: dict[str, str] | None = None) -> dict[str, str]:
    """Add W3C ``traceparent``/``tracestate`` for the active span (no-op without one)."""
    carrier = carrier if carrier is not None else {}
    propagate.inject(carrier)
    return carrier


def extract_context(headers: Mapping[str, Any] | None) -> otel_context.Context:
    carrier = {str(key).lower(): str(value) for key, value in (headers or {}).items() if value}
    return propagate.extract(carrier)


@contextmanager
def span_from_headers(
    name: str, headers: Mapping[str, Any] | None, *, kind: SpanKind = SpanKind.CONSUMER, **attributes: Any
) -> Iterator[Span]:
    """Continue a remote trace (Kafka record, n8n callback) in a new span."""
    with tracer().start_as_current_span(
        name, context=extract_context(headers), kind=kind, attributes=attributes
    ) as span:
        yield span


def current_trace_id() -> str:
    """The active OpenTelemetry trace id as 32 hex characters, or ``""``."""
    span_context = trace.get_current_span().get_span_context()
    return format(span_context.trace_id, "032x") if span_context.is_valid else ""
