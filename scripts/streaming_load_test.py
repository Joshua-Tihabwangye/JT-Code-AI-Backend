#!/usr/bin/env python3
"""Exercise authenticated SSE chat streams against a deployed JT-Code API."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import sys
import time
import urllib.error
import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="For example: https://staging-api.example.com")
    parser.add_argument("--token", required=True, help="Short-lived Supabase access token")
    parser.add_argument(
        "--request-id",
        action="append",
        required=True,
        help="A tenant-authorized chat request UUID; repeat for multiple requests.",
    )
    parser.add_argument("--connections", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=100.0)
    parser.add_argument("--max-first-event-ms", type=float, default=5_000.0)
    return parser.parse_args()


def read_first_event(base_url: str, token: str, request_id: str, timeout: float) -> tuple[int, float, str]:
    url = f"{base_url.rstrip('/')}/api/v1/chat/requests/{request_id}/stream/"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310: operator-supplied HTTPS URL
            content_type = response.headers.get_content_type()
            if response.status != 200 or content_type != "text/event-stream":
                return response.status, 0.0, f"unexpected response content type: {content_type}"
            while line := response.readline():
                decoded = line.decode("utf-8", errors="replace").strip()
                if decoded.startswith("event: "):
                    return response.status, (time.perf_counter() - started) * 1000, decoded[7:]
            return response.status, 0.0, "stream ended before an event"
    except urllib.error.HTTPError as exc:
        return exc.code, 0.0, f"HTTP {exc.code}"
    except (TimeoutError, urllib.error.URLError) as exc:
        return 0, 0.0, str(exc)


def main() -> int:
    args = parse_args()
    if args.connections < 1 or args.concurrency < 1:
        raise SystemExit("connections and concurrency must be positive")
    request_ids = [args.request_id[index % len(args.request_id)] for index in range(args.connections)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        results = list(
            executor.map(
                lambda request_id: read_first_event(
                    args.base_url, args.token, request_id, args.timeout_seconds
                ),
                request_ids,
            )
        )
    latencies = [latency for status, latency, _ in results if status == 200]
    failures = [
        {"status": status, "detail": detail}
        for status, _, detail in results
        if status != 200 or detail not in {"status", "completed", "failed", "cancelled", "heartbeat"}
    ]
    report = {
        "connections": args.connections,
        "successfulConnections": len(latencies),
        "failures": failures,
        "firstEventP50Ms": round(statistics.median(latencies), 2) if latencies else None,
        "firstEventMaxMs": round(max(latencies), 2) if latencies else None,
    }
    print(json.dumps(report, indent=2))
    if failures or not latencies or max(latencies) > args.max_first_event_ms:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
