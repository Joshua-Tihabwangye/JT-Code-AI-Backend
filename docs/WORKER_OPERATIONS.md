# Phase 5 worker operations

Phase 5 uses Redis-backed Celery queues and durable records in `jobs_job`; it does not require Docker.

## Queue topology

| Queue | Workload |
| --- | --- |
| `jobs.analysis` | Native AI, RAG, and research jobs |
| `jobs.ingestion` | Knowledge ingestion |
| `jobs.visualization` | Image, document, and file-conversion work |
| `jobs.default` | Other durable jobs |

Run a separately named worker for each workload.

```bash
celery -A config worker -Q jobs.analysis -n analysis@%h
celery -A config worker -Q jobs.ingestion -n ingestion@%h
celery -A config worker -Q jobs.visualization -n visualization@%h
celery -A config worker -Q jobs.default -n default@%h
celery -A config beat
```

## Reliability contract

- A job stores queue, Celery task ID, progress, retry count, retry limit, retry time, and cancellation time.
- Workers acknowledge late and reject lost-worker messages. A periodic recovery task requeues native jobs that remain running past `JOB_STALLED_TIMEOUT_SECONDS` (660 seconds by default).
- Transient connection and timeout failures retry with bounded exponential backoff plus jitter.
- A database lock claims a queued job before it runs; duplicate deliveries of running or terminal jobs are harmless.
- Cancellation marks the durable job first, then revokes its queued Celery task. A running handler checks durable cancellation before finalizing.

## Monitoring and recovery

Staff users can query `GET /api/v1/jobs/queue-metrics/` for durable queue depth. It reports queued, running/validating, and waiting-for-approval counts by queue; it does not depend on transient broker inspection.

For a worker-crash drill:

1. Submit a native job and record its `celery_task_id` and `queue_name`.
2. Stop the worker while the task is running.
3. Confirm the stale-job recovery task requeues the job after the configured grace period.
4. Confirm only one `JobStep` runs and the final `Job` reaches a terminal state.
5. Repeat with cancellation; the job must remain `cancelled` even if a prior delivery resumes.

Run this drill against a real Redis-backed staging environment before production. The automated suite proves durable state transitions but intentionally does not claim to simulate a process kill or Redis redelivery.

## Redis key isolation

The Django cache uses `jt-code:cache`; rate limits use `jt-code:rate-limit`; job locks use `jt-code:job-lock`. Keep these prefixes distinct if a shared Redis cluster is used.
