"""Production release gate (Phase 18 exit criterion): SLO, security and recovery evidence.

    python manage.py release_gate --prometheus-url https://prometheus.internal --max-age-days 30

Fails unless, for this environment:

* every required verification kind has a **passing run within the window**
  (restore, DLQ, saturation and chaos drills; load, spike, soak and streaming
  tests; RAG quality and security evaluations; a capacity audit);
* the **SLOs** in docs/SLOs.md hold over their windows, when a Prometheus URL
  is given (availability, p95/p99 latency, outbox backlog, Stripe failures);
* no verification of a required kind **failed after** its last pass.

The verdict is stored as a ``release_gate`` run and printed as JSON for the
release record.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.operations.evidence import environment_name, recorded
from apps.operations.models import VerificationRun

REQUIRED_KINDS = (
    VerificationRun.Kind.RESTORE_DRILL,
    VerificationRun.Kind.DLQ_DRILL,
    VerificationRun.Kind.SATURATION_DRILL,
    VerificationRun.Kind.LOAD_TEST,
    VerificationRun.Kind.SPIKE_TEST,
    VerificationRun.Kind.SOAK_TEST,
    VerificationRun.Kind.STREAMING_TEST,
    VerificationRun.Kind.RAG_EVALUATION,
    VerificationRun.Kind.RAG_SECURITY,
    VerificationRun.Kind.CHAOS_EXPERIMENT,
    VerificationRun.Kind.CAPACITY_AUDIT,
)

# (name, PromQL, comparison, threshold) - targets from docs/SLOs.md.
SLO_QUERIES = (
    (
        "availability_30d",
        '1 - (sum(increase(jt_http_requests_total{status=~"5.."}[30d]))'
        " / clamp_min(sum(increase(jt_http_requests_total[30d])), 1))",
        ">=",
        0.999,
    ),
    (
        "latency_p95_7d_seconds",
        "histogram_quantile(0.95, sum by (le) "
        '(rate(jt_http_request_duration_seconds_bucket{route!~".*/stream/"}[7d])))',
        "<=",
        0.8,
    ),
    (
        "latency_p99_7d_seconds",
        "histogram_quantile(0.99, sum by (le) "
        '(rate(jt_http_request_duration_seconds_bucket{route!~".*/stream/"}[7d])))',
        "<=",
        2.0,
    ),
    ("outbox_backlog_max_1d", 'max_over_time(jt_outbox_events{state="pending"}[1d])', "<=", 1000),
    ("stripe_events_failed", 'max(jt_stripe_events_unprocessed{status="failed"}) or vector(0)', "<=", 0),
    ("dead_letters_unreplayed", "max(jt_dead_letter_events) or vector(0)", "<=", 0),
)


def evaluate_slos(prometheus_url: str, token: str = "") -> tuple[dict[str, Any], list[str]]:  # nosec B107
    results: dict[str, Any] = {}
    failures: list[str] = []
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(timeout=30, headers=headers) as client:
        for name, query, comparison, threshold in SLO_QUERIES:
            response = client.get(f"{prometheus_url.rstrip('/')}/api/v1/query", params={"query": query})
            response.raise_for_status()
            series = response.json().get("data", {}).get("result", [])
            if not series:
                failures.append(f"{name}: no data")
                results[name] = None
                continue
            value = float(series[0]["value"][1])
            results[name] = value
            ok = value >= threshold if comparison == ">=" else value <= threshold
            if not ok:
                failures.append(f"{name}: {value} violates {comparison} {threshold}")
    return results, failures


def evaluate_evidence(
    *, max_age_days: int, environment: str, kinds: tuple[str, ...]
) -> tuple[dict[str, Any], list[str]]:
    cutoff = timezone.now() - timedelta(days=max_age_days)
    evidence: dict[str, Any] = {}
    failures: list[str] = []
    runs = VerificationRun.objects.filter(started_at__gte=cutoff)
    if environment:
        runs = runs.filter(environment=environment)
    for kind in kinds:
        last_pass = (
            runs.filter(kind=kind, status=VerificationRun.Status.PASSED).order_by("-started_at").first()
        )
        if last_pass is None:
            failures.append(f"{kind}: no passing run in the last {max_age_days} days")
            evidence[kind] = None
            continue
        later_failure = runs.filter(
            kind=kind, status=VerificationRun.Status.FAILED, started_at__gt=last_pass.started_at
        ).exists()
        if later_failure:
            failures.append(f"{kind}: failed after its last pass ({last_pass.started_at:%Y-%m-%d})")
        evidence[kind] = {"runId": str(last_pass.id), "at": last_pass.started_at.isoformat()}
    return evidence, failures


class Command(BaseCommand):
    help = "Release gate: required verification evidence plus measured SLO compliance."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--max-age-days", type=int, default=30)
        parser.add_argument("--environment", default=None, help="Defaults to this deployment's environment.")
        parser.add_argument("--prometheus-url", default="")
        parser.add_argument("--prometheus-token", default="")
        parser.add_argument("--skip-kind", action="append", default=[], help="Explicitly waived kinds.")

    def handle(self, *args: Any, **options: Any) -> None:
        environment = environment_name() if options["environment"] is None else options["environment"]
        kinds = tuple(kind for kind in REQUIRED_KINDS if kind not in set(options["skip_kind"]))
        with recorded(
            VerificationRun.Kind.RELEASE_GATE,
            parameters={**options, "prometheus_token": ""},  # nosec B105 - redacted
        ) as run:
            evidence, failures = evaluate_evidence(
                max_age_days=options["max_age_days"], environment=environment, kinds=kinds
            )
            run.summary = {"environment": environment, "evidence": evidence, "waived": options["skip_kind"]}
            if options["prometheus_url"]:
                slos, slo_failures = evaluate_slos(options["prometheus_url"], options["prometheus_token"])
                run.summary["slos"] = slos
                failures += slo_failures
            else:
                failures.append("slos: no --prometheus-url given; SLO compliance not measured")
            for failure in failures:
                run.fail(failure)
        self.stdout.write(
            json.dumps({**run.summary, "failures": run.failures, "passed": not run.failures}, indent=2)
        )
        if run.failures:
            raise CommandError(f"Release gate failed ({len(run.failures)} problems).")
