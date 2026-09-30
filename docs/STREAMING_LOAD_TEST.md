# Streaming load test

Run this only against a staging environment using a short-lived Supabase JWT for a dedicated
test tenant. It makes read-only SSE requests to existing chat requests; it does not submit
prompts or create jobs.

```bash
python3 scripts/streaming_load_test.py \
  --base-url https://staging-api.example.com \
  --token "$SUPABASE_ACCESS_TOKEN" \
  --request-id "$COMPLETED_OR_RUNNING_REQUEST_ID" \
  --connections 50 \
  --concurrency 10 \
  --max-first-event-ms 5000
```

The command exits non-zero if an SSE connection is unauthorized, fails, has the wrong content
type, ends before an event, emits an unexpected event type, or exceeds the first-event latency
budget. Keep the JSON output with the release evidence. Start at 20 connections, then test at
the expected steady-state concurrency and at least 2× that value. Record API CPU/memory, open
connections, database connection use, Redis/Celery health, error rate, and p50/max first-event
latency alongside the result.

This harness checks the Phase 4 SSE transport contract. It does not replace an end-to-end load
test of model-provider latency or job execution; those should use controlled provider mocks or
an approved staging cost budget.
