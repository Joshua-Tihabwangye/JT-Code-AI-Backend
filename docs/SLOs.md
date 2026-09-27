# Initial Service Level Objectives (SLOs)

Phase 0 artifact. These objectives are the agreed baseline before production
launch and are revisited with measured data (Phase 15 observability, Phase 18
production verification).

## Objectives

| Identifier | Objective | Target window | Error budget |
|-----------|-----------|---------------|--------------|
| AVI-API | Availability of `/api/v1/health/live` (liveness) | ≥ 99.9% monthly | 0.1% |
| AVI-READY | Availability of `/api/v1/health/ready` (DB + Redis reachable) | ≥ 99.5% monthly | 0.5% |
| LAT-P95 | p95 latency of non-streaming API calls | ≤ 800 ms | measured |
| LAT-P99 | p99 latency of non-streaming API calls | ≤ 2 s | measured |
| CHAT-P95 | p95 time-to-complete for chat/generation jobs (incl. fallbacks) | ≤ 15 s | measured |
| OUT-DRAIN | Outbox events published to Kafka (p95 age from commit) | ≤ 5 s | measured |
| JOB-P95 | Standard background jobs complete (p95) | ≤ 10 min | measured |
| DUR-BACKUP | Backup/PITR point-in-time recovery window | ≥ 24 h age coverage | n/a |
| REC-RTO | Recovery time objective (restore + ready) | ≤ 4 h | per DR drill |
| REC-RPO | Recovery point objective (max data loss) | ≤ 1 h | per DR drill |

## SLI measurement notes

- **Availability:** probe `/api/v1/health/live` and `/api/v1/health/ready` every 15 s; an SLI
  error is any non-200 response. Request-based rather than synthetic where the
  frontend gateway exposes healthy request counters.
- **Latency:** from Sentry transaction `duration`/`http.transaction` traces,
  computed over authenticated API requests only (exclude static/health).
- **Outbox drain:** `events.outboxevent.published_at - created_at` over
  published rows.
- **Jobs:** `jobs.job.completed_at - created_at` for terminal-state jobs.

## Ownership and review

- Owner: backend/platform engineer.
- Evaluate monthly; breach of an error budget triggers a review runbook entry
  and, where needed, an SLO amendment (new review, not silent change).

Companion docs: `docs/SECURITY.md` (data classification + threat model),
`docs/INVENTORY.md` (inventory).