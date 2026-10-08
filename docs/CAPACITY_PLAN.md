# Capacity plan (Phase 19)

## Method

Capacity is **measured, then extrapolated**; it is never guessed.

1. **Per-unit throughput.** Measure each unit at the SLO on staging, which runs
   the production topology:
   * **API:** `loadtests/mix-100k.json` gives requests/s per API pod at
     p95 ≤ 800 ms and error rate < 1% (load evidence).
   * **Workers:** `manage.py saturation_drill` gives jobs/s per worker pod for
     each queue (saturation evidence).
   * **Kafka:** the same drill (`--kafka-events`) gives events/s per partition.
   * **Streaming:** `loadtests/streaming.json` gives concurrent SSE streams per
     API pod at first-event p95 ≤ 5 s.
2. **Plan.** `python manage.py capacity_plan --users 100000` reads the latest
   passing evidence and computes the following for the user target, with 1.5×
   headroom:
   * API pods;
   * worker pods per queue;
   * Kafka partitions.

   It also flags any gap against the production autoscaling ceilings.
3. **Budget check.** `python manage.py capacity_audit --pooler-max-clients <N>`
   compares the worst-case database client connections (every Deployment at
   its ceiling) with the Supavisor client limit. It also audits indexes, scan
   hotspots and partition candidates (ADR-006).
4. **Sustained load.** `loadtests/soak.json` (4 h) must hold p95 within 1.5× of
   its first 20% (no leaks, pool exhaustion or queue drift). `loadtests/spike.json`
   must recover after a 20× surge.

`manage.py release_gate` requires all of this evidence to be passing and recent
before launch.

## Target workload: 100,000 registered users

| Assumption | Value | Basis |
| --- | --- | --- |
| Daily active | 20% → 20,000 | B2B SaaS typical; revise from analytics |
| Peak concurrency | 10% of daily active → 2,000 active users | |
| Requests per active user | 12 / min (one every 5 s) | Frontend polling, navigation, search |
| Peak API rate | **400 req/s** | 2,000 × 12 / 60 |
| Generations (chat, agent, RAG) | 6 / active user / hour → 3.3 jobs/s | `jobs.analysis` |
| Concurrent SSE streams | up to 2,000 | one per active chat |

## Measured findings so far

**Database connections (measured 2026-10-05).** At the production overlay's
ceilings, the worst-case client connections are **383**:

| Component | Connections |
| --- | --- |
| API: 20 pods × 2 | 40 |
| AI workers: 20 × 9 | 180 |
| Other worker pools | 160 |
| Beat, consumer, migrate | 3 |

The current Supabase project has `max_connections = 60`, so it **must** be used
through the transaction pooler (`DATABASE_POOLER_MODE=transaction`,
`CONN_MAX_AGE=0`), and the Supavisor client limit must be at least 400. That
means **Small compute or larger**, or reducing the AI worker ceiling and
concurrency.

**Indexes.** None missing and none unused. No scan hotspots and no partition
candidates (see ADR-006).

**Throughput.** **Not yet measured.** Run the staging scenarios (production
verification workflow, suites `mix-100k`, `streaming`, `soak`, `spike`, plus
the drills) and then `capacity_plan`. This document is updated from that output.

## Scaling levers (in order)

1. **Worker pools scale on queue depth** (KEDA, per queue). Raise
   `maxReplicaCount` in the production overlay, checking the database
   connection budget each time.
2. **API scales on CPU and memory** (HPA). SSE streams are mostly idle I/O, so
   memory is the usual limit.
3. **Kafka partitions per topic:** `KAFKA_TOPIC_PARTITIONS` (default) and
   `KAFKA_TOPIC_PARTITIONS_OVERRIDES` (per hot topic), applied by
   `manage.py ensure_kafka_topics`. Consumer replicas must not exceed the
   partition count.
4. **Database:** Supabase compute size (connections, CPU), read replicas for
   analytics views, then partitioning (ADR-006).
5. **Cost guardrails:**
   * per-tenant spending limits and quotas (Phase 13);
   * cost-anomaly detection (`usage.cost.anomaly`, alert `CostAnomalyDetected`);
   * agent and RAG cost ceilings.
