# ADR-006: Partition high-volume tables only on measured need

- **Status:** Accepted
- **Date:** 2026-10-05
- **Related:** Production backlog Phase 19; `manage.py capacity_audit`

## Context

These append-heavy tables grow with usage:

* `conversations_message`
* `conversations_chatrequest`
* `ai_gateway_modelrun`
* `usage_usagerecord`
* `billing_creditledger`
* `governance_auditevent`
* `events_outboxevent`
* `events_consumedevent`
* `knowledge_chunk`
* `jobs_job`

Partitioning in PostgreSQL (by month on `created_at`) makes retention a cheap
`DETACH`/`DROP` and keeps hot indexes small. It also has costs:

* Primary keys must include the partition key.
* Unique constraints become per partition.
* Foreign keys *into* partitioned tables need PostgreSQL ≥ 12 semantics.
* Django migrations cannot express it natively.

Partitioning prematurely adds risk with no benefit.

## Decision

1. **Measure first.** `capacity_audit` reports each append-heavy table that
   crosses **50 M rows or 50 GB** (`partitionCandidates`); the weekly
   verification workflow records this. Only a reported candidate is partitioned.
2. **Retention before partitioning.** Bounded tables stay unpartitioned and are
   pruned instead:
   * `events_outboxevent` (`prune_published_outbox_events`);
   * `events_consumedevent` (consumer retention);
   * `orchestration_workflowcallback` (14 days);
   * `governance_auditevent` (retention rules).
3. **How to partition a candidate:** declarative monthly range partitions on
   `created_at`, managed by `pg_partman` (available on Supabase):
   1. Create the partitioned shadow table with `(id, created_at)` as the primary key.
   2. Backfill in batches.
   3. Swap the tables in a short maintenance window (`READ_ONLY_MODE=true`).
   4. Keep the append-only triggers on every partition.

   Django keeps `id` as the model primary key; the composite key exists only in
   the database (`managed` migration with `SeparateDatabaseAndState`).
4. **Ledger tables** (`usage_usagerecord`, `billing_creditledger`) are partitioned
   last. Their per-tenant aggregates read by `(organization, period)` indexes,
   which scale to hundreds of millions of rows.

## Measured state (2026-10-05)

`capacity_audit` against the Supabase project found:

* no candidates;
* largest table under 1 MB;
* zero unindexed foreign keys, zero unused indexes and no sequential-scan
  hotspots.

**No table is partitioned now.** Re-evaluate when a candidate appears.

## Consequences

* No migration risk today; the procedure is documented before it is needed.
* Retention jobs must keep running. Their failures alert through `CeleryTaskFailures`.

## Verification

- `python manage.py capacity_audit` reports `partitionCandidates`, unindexed foreign
  keys and unused indexes, and records `capacity_audit` evidence that the release
  gate requires (`apps/operations/capacity.py`).
- `tests/test_phase19_scale.py` runs the audit against the live Supabase database.
