# ADR-005: Kafka, Celery and n8n responsibility boundaries

- **Status:** Accepted
- **Date:** 2026-09-20
- **Related:** Production backlog Phase 0, Phases 5, 6, 16

## Context

Async work in the product spans Django-owned background jobs, a Kafka event
platform for durable integration events, and n8n for external workflow
orchestration. Without explicit boundaries these systems overlap and fight
over canonical state.

## Decision

- **Django/PostgreSQL is the single source of truth.** Redis, Kafka and n8n
  never hold canonical business state.
- **Redis** = cache + Celery transport only. No durable business state lives in
  Redis (keys prefixed `jt-code`). It is not a database.
- **Celery** = Django-owned background jobs and schedules. Durable job state
  lives in `apps/jobs.Job` (status, progress, retry, cancel); Celery Beat
  drives the schedule (`config/settings/base.py`); tasks must be idempotent and
  acknowledgements late (`CELERY_TASK_ACKS_LATE`).
- **Kafka** = durable event bus for domain/integration events. Events are
  written to the PostgreSQL transaction outbox
  (`apps.events.outbox` → `apps.events.OutboxEvent`) inside the same transaction
  that mutates state, then published by the outbox publisher task
  (`apps.events.tasks.publish_outbox_batch`) to Kafka topics prefixed
  `KAFKA_TOPIC_PREFIX`. Consumers (`apps.events.management.commands.run_kafka_consumer`)
  must be idempotent and manual-commit.
- **n8n** = external workflow orchestration only. Django signs
  Django→n8n requests and verifies n8n→Django callbacks; workflow events are
  published back through Kafka; failures relay to Sentry
  (`apps.core.views.N8nSentryRelayView`). Anything that is core business policy
  is implemented as a Django job instead of an n8n workflow.
- AI-task execution (`GENERAL_QUESTION`, `RAG_QUERY`, `SEARCH_RESEARCH`) runs
  inside Django workers via `apps.jobs.executor`, emitting
  `jobs.job.completed/jobs.job.failed` outbox events.

## Consequences

- **Positive:** clear responsibility map; events are reliable and replayable;
  workflows cannot overwrite canonical state.
- **Negative:** outbox adds a publishing step and latency; n8n integration must
  be secured with signed payloads (Phase 16).
- **Action (Phases 5/6/16):** Celery queue segregation, retries/backoff; event
  schema versioning, consumer groups, DLQ and correlation-ID propagation; n8n
  queue-mode + signed callbacks + versioned workflows.

## Verification

- `apps/events/outbox.py`, `apps/events/tasks.py`, `apps/jobs/executor.py`, and
  the beat schedule in `config/settings/base.py` implement the boundary.
- Phase 16 (`apps/orchestration`, [N8N_ORCHESTRATION.md](../N8N_ORCHESTRATION.md)):
  n8n runs in queue mode with its own Supabase schema; Django signs dispatches
  (`N8N_DISPATCH_SECRET`) and verifies signed, nonce-bound, attempt-bound
  callbacks (`N8N_WEBHOOK_SECRET`). Attempts, retries, deliveries and
  automations are Django rows. Workflow events go through the outbox to
  Kafka, and the n8n error workflow relays to Sentry. Workflow definitions are
  versioned in `n8n/workflows/` and deployed with `manage.py n8n_workflows push`.