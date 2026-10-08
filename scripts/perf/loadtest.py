#!/usr/bin/env python3
"""JT-Code load, spike, soak and streaming test harness (Phases 18-19).

    python scripts/perf/loadtest.py --base-url https://api.staging.example.com \
        --scenario loadtests/load.json --token "$SUPABASE_ACCESS_TOKEN" \
        --organization "$ORG_ID" --var chatRequestId=<uuid> --out load-report.json

A scenario (``loadtests/*.json``) declares ramping stages of virtual users, a
weighted request mix (optionally SSE streams), think time and SLO thresholds.
The report (JSON) lists per-request latency percentiles, error rates,
throughput, SSE time-to-first-event, per-minute p95 (soak drift) and a
``passed`` verdict; store it as evidence with ``manage.py record_evidence``.

Run against staging with dedicated test tenants. One process sustains a few
thousand requests/second; for more, run several processes and pass all reports
to ``manage.py capacity_plan``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


@dataclass
class Sample:
    name: str
    started: float
    latency_ms: float
    status: int
    ok: bool
    first_event_ms: float | None = None


@dataclass
class Collector:
    samples: list[Sample] = field(default_factory=list)
    peak_users: int = 0

    def add(self, sample: Sample) -> None:
        self.samples.append(sample)


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * quantile) - 1))], 2)


def target_users(stages: list[dict[str, Any]], elapsed: float) -> int | None:
    """Linear ramp between stage targets (like k6 ramping VUs); None once finished."""
    start_users, clock = 0.0, 0.0
    for stage in stages:
        duration = float(stage["duration"])
        if elapsed < clock + duration:
            fraction = (elapsed - clock) / duration if duration else 1.0
            return round(start_users + (float(stage["users"]) - start_users) * fraction)
        clock += duration
        start_users = float(stage["users"])
    return None


def substitute(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        for key, replacement in variables.items():
            value = value.replace("{" + key + "}", replacement)
        return value
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    return value


async def run_request(client: httpx.AsyncClient, spec: dict[str, Any], collector: Collector) -> None:
    started = time.perf_counter()
    wall = time.time()
    expected = set(spec.get("expectStatus", [200, 201, 202, 204]))
    try:
        if spec.get("stream"):
            first_event = None
            async with client.stream(
                spec["method"], spec["path"], headers={"Accept": "text/event-stream"}
            ) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith(("event:", "data:")) and first_event is None:
                        first_event = (time.perf_counter() - started) * 1000
                        if not spec.get("readToEnd"):
                            break
            collector.add(
                Sample(
                    spec["name"],
                    wall,
                    (time.perf_counter() - started) * 1000,
                    resp.status_code,
                    resp.status_code in expected and first_event is not None,
                    first_event,
                )
            )
            return
        response = await client.request(spec["method"], spec["path"], json=spec.get("json"))
        collector.add(
            Sample(
                spec["name"],
                wall,
                (time.perf_counter() - started) * 1000,
                response.status_code,
                response.status_code in expected,
            )
        )
    except httpx.HTTPError:
        collector.add(Sample(spec["name"], wall, (time.perf_counter() - started) * 1000, 0, False))


async def virtual_user(
    client: httpx.AsyncClient, scenario: dict[str, Any], collector: Collector, stop: asyncio.Event
) -> None:
    requests = scenario["requests"]
    weights = [float(r.get("weight", 1)) for r in requests]
    low, high = scenario.get("thinkTimeSeconds", [1, 3])
    while not stop.is_set():
        spec = random.choices(requests, weights=weights)[0]  # noqa: S311 - load mix, not security
        await run_request(client, spec, collector)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=random.uniform(low, high))  # noqa: S311


async def run_scenario(scenario: dict[str, Any], *, base_url: str, headers: dict[str, str]) -> Collector:
    collector = Collector()
    limits = httpx.Limits(max_connections=None, max_keepalive_connections=2000)
    timeout = httpx.Timeout(float(scenario.get("requestTimeoutSeconds", 30)))
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, limits=limits, timeout=timeout
    ) as client:
        users: list[tuple[asyncio.Task[None], asyncio.Event]] = []
        started = time.monotonic()
        while True:
            target = target_users(scenario["stages"], time.monotonic() - started)
            if target is None:
                break
            while len(users) < target:
                stop = asyncio.Event()
                users.append((asyncio.create_task(virtual_user(client, scenario, collector, stop)), stop))
            while len(users) > target:
                task, stop = users.pop()
                stop.set()
            collector.peak_users = max(collector.peak_users, len(users))
            await asyncio.sleep(0.5)
        for _task, stop in users:
            stop.set()
        await asyncio.gather(*(task for task, _ in users), return_exceptions=True)
    return collector


def build_report(scenario: dict[str, Any], collector: Collector, duration: float) -> dict[str, Any]:
    thresholds = scenario.get("thresholds", {})
    samples = collector.samples
    by_name: dict[str, list[Sample]] = defaultdict(list)
    for sample in samples:
        by_name[sample.name].append(sample)
    requests: dict[str, Any] = {}
    for name, group in sorted(by_name.items()):
        latencies = [s.latency_ms for s in group]
        streams = [s.first_event_ms for s in group if s.first_event_ms is not None]
        requests[name] = {
            "count": len(group),
            "errors": sum(not s.ok for s in group),
            "statusCounts": {
                str(code): sum(s.status == code for s in group) for code in sorted({s.status for s in group})
            },
            "p50Ms": percentile(latencies, 0.50),
            "p95Ms": percentile(latencies, 0.95),
            "p99Ms": percentile(latencies, 0.99),
            "firstEventP95Ms": percentile(streams, 0.95) if streams else None,
        }
    plain = [s.latency_ms for s in samples if s.first_event_ms is None and s.ok]
    streams = [s.first_event_ms for s in samples if s.first_event_ms is not None]
    total = len(samples)
    errors = sum(not s.ok for s in samples)
    minute: dict[int, list[float]] = defaultdict(list)
    origin = min((s.started for s in samples), default=0.0)
    for sample in samples:
        if sample.ok and sample.first_event_ms is None:
            minute[int((sample.started - origin) // 60)].append(sample.latency_ms)
    timeline = [
        {"minute": m, "p95Ms": percentile(v, 0.95), "count": len(v)} for m, v in sorted(minute.items())
    ]
    summary = {
        "requests": total,
        "errors": errors,
        "errorRate": round(errors / total, 5) if total else 1.0,
        "throughputRps": round(total / duration, 2) if duration else 0.0,
        "peakUsers": collector.peak_users,
        "p50Ms": percentile(plain, 0.50),
        "p95Ms": percentile(plain, 0.95),
        "p99Ms": percentile(plain, 0.99),
        "streamFirstEventP95Ms": percentile(streams, 0.95) if streams else None,
    }
    failures: list[str] = []
    if total == 0:
        failures.append("no requests were made")
    checks = (
        ("errorRate", summary["errorRate"], "max"),
        ("p95Ms", summary["p95Ms"], "max"),
        ("p99Ms", summary["p99Ms"], "max"),
        ("streamFirstEventP95Ms", summary["streamFirstEventP95Ms"], "max"),
        ("minRps", summary["throughputRps"], "min"),
    )
    for key, value, kind in checks:
        if key not in thresholds or value is None:
            continue
        limit = float(thresholds[key])
        if (kind == "max" and value > limit) or (kind == "min" and value < limit):
            failures.append(f"{key}={value} violates {'<=' if kind == 'max' else '>='} {limit}")
    if "maxDegradation" in thresholds and len(timeline) >= 4:
        head = [t["p95Ms"] for t in timeline[: max(1, len(timeline) // 5)] if t["p95Ms"]]
        tail = [t["p95Ms"] for t in timeline[-max(1, len(timeline) // 5) :] if t["p95Ms"]]
        if head and tail:
            degradation = (sum(tail) / len(tail)) / max(sum(head) / len(head), 1e-6)
            summary["soakDegradation"] = round(degradation, 3)
            if degradation > float(thresholds["maxDegradation"]):
                failures.append(f"p95 degraded {degradation:.2f}x over the run (soak)")
    return {
        "scenario": scenario["name"],
        "kind": scenario.get("kind", "load_test"),
        "durationSeconds": round(duration, 1),
        "summary": summary,
        "requests": requests,
        "timeline": timeline,
        "thresholds": thresholds,
        "failures": failures,
        "passed": not failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--scenario", required=True, type=Path)
    parser.add_argument("--token", default="", help="Supabase access token of a dedicated test user.")
    parser.add_argument("--organization", default="", help="X-Organization-ID of the test tenant.")
    parser.add_argument(
        "--var", action="append", default=[], help="key=value substituted into {key} in paths."
    )
    parser.add_argument("--duration-scale", type=float, default=1.0, help="Shrink/stretch every stage.")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    scenario = json.loads(args.scenario.read_text())
    variables = dict(item.split("=", 1) for item in args.var)
    scenario["requests"] = substitute(scenario["requests"], variables)
    for stage in scenario["stages"]:
        stage["duration"] = float(stage["duration"]) * args.duration_scale
    headers = {"User-Agent": "jt-code-loadtest/1.0"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    if args.organization:
        headers["X-Organization-ID"] = args.organization
    started = time.monotonic()
    collector = asyncio.run(run_scenario(scenario, base_url=args.base_url, headers=headers))
    report = build_report(scenario, collector, time.monotonic() - started)
    report["parameters"] = {
        "baseUrl": args.base_url,
        "scenario": str(args.scenario),
        "scale": args.duration_scale,
    }
    output = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(output)
    print(output)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
