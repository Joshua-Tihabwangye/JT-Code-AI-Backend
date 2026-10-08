# Dependency and regional failure plan (Phase 19)

Each dependency has a known failure behaviour. The ones that can be simulated
in-process are tested in CI (`tests/test_phase18_verification.py`, the Phase 7
and Phase 5 tests); the others are rehearsed with Chaos Mesh on staging
(`infra/chaos`).

| Dependency | Failure | Behaviour | Data loss | Test |
| --- | --- | --- | --- | --- |
| **Supabase PostgreSQL** | Outage or failover | `/health/ready` 503, so pods are pulled from the load balancer; writes fail fast. Planned failover: `READ_ONLY_MODE=true` (503 + `Retry-After` on writes, reads served) | None after commit; RPO ≤ 1 h from PITR | readiness test; restore drill |
| Supabase region loss | Region down | Restore PITR into a new project in another region (BACKUP_RESTORE_RUNBOOK), repoint `DATABASE_URL` through Terraform (`supabase_project`), redeploy | RPO ≤ 1 h; RTO ≤ 4 h | restore drill (quarterly) |
| **Redis** (cache, broker) | Outage | Rate limits fail open (alert); jobs and chat requests stay durable in PostgreSQL and are re-dispatched by beat sweeps when Redis returns | None | `test_rate_limits_fail_open_when_redis_is_down`, broker-outage tests, chaos `redis-network-loss` |
| **Kafka** | Outage | The outbox keeps events (`pending`); the publisher retries with backoff; n8n deliveries are independent (created in the same DB transaction) | None | `test_kafka_outage_keeps_events_durable_until_recovery` |
| **AI providers** | Outage, latency or rate limits | Gateway retries, then a circuit breaker, then the next model in the policy (fallback). If all fail: error code, credit hold released | None | Phase 7 tests; chaos `provider-latency`; `test_image_provider_outage_returns_503_and_releases_the_hold` |
| Embeddings | Outage | Ingestion retries with backoff; search degrades to full-text only (`degraded: ["semantic"]`) | None | Phase 10 tests |
| **Stripe** | Outage | Checkout and top-up return errors; webhooks are redelivered by Stripe and retried; hourly reconciliation converges subscription and payment state | None | Phase 14 tests |
| **Supabase Storage** | Outage | Uploads and renders return 503; pending deletes retried by the sweeper | None | Phase 11 tests |
| **n8n** | Outage | Dispatches retried with backoff; silent runs time out and are retried; jobs fail cleanly after `maxAttempts` (credits released) | None | Phase 16 tests |
| **Cloudflare** | Outage | DNS-only fallback is possible, but turning off `CLOUDFLARE_ENFORCE_ORIGIN` exposes the origin. Leave it on; accept edge downtime | None | n/a |
| Kubernetes node | Node loss | PodDisruptionBudgets, topology spread, `acks_late` + stalled-job recovery | None | chaos `worker-pod-kill` |

## Multi-region posture

The current tier is **single region, recover-by-restore**:

* RTO ≤ 4 h;
* RPO ≤ 1 h.

Going active-passive across regions is part of the 99.95% tier decision below.
