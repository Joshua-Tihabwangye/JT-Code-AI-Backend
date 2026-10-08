# Load scenarios

Run these with `scripts/perf/loadtest.py`; the [deployment runbook](../docs/DEPLOYMENT.md)
and [production verification](../docs/PRODUCTION_VERIFICATION.md) explain where.

| Scenario | Purpose | Evidence kind |
| --- | --- | --- |
| `smoke.json` | Harness check (30 s, 5 users) | `load_test` |
| `load.json` | Steady load at expected peak (300 users) against the SLOs | `load_test` |
| `spike.json` | 20× surge in 10 s, then recovery | `spike_test` |
| `soak.json` | 4 h at steady load; p95 must not drift more than 1.5× (leaks, pool exhaustion) | `soak_test` |
| `streaming.json` | 500 concurrent SSE chat streams, first event p95 ≤ 5 s | `streaming_test` |
| `mix-100k.json` | Realistic 100K-user peak mix (about 2,000 active users) for the capacity plan | `load_test` |

Every scenario uses a dedicated test tenant. Read-only mixes create no billable
work. To include chat streams, pass `--var chatRequestId=<existing request id>`.
