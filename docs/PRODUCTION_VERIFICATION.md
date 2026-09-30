# Production verification runbook

This backend uses Supabase PostgreSQL, managed Redis, and managed Kafka. It does not require Docker. Never put live credentials in this repository, issue tracker, CI output, or this document.

## Isolated targets

Provision three different, disposable Supabase PostgreSQL databases/projects and store only their URLs in GitHub Actions secrets:

| Secret | Purpose | Allowed writes |
| --- | --- | --- |
| `SUPABASE_CI_DATABASE_URL` | migration and backfill evidence | migrations and deterministic restore fixture |
| `SUPABASE_CI_TEST_DATABASE_URL` | pytest-created test database | test schema/data only |
| `SUPABASE_CI_RESTORE_DATABASE_URL` | manual restore drill | may be overwritten by `pg_restore` only after explicit operator confirmation |

Do not point any of these at staging or production. The CI workflow deliberately does **not** run a restore because restore overwrites its target.

Also configure these GitHub secrets for CI: `CI_REDIS_URL`, `CI_CELERY_BROKER_URL`, `CI_CELERY_RESULT_BACKEND`, `CI_WEBHOOK_ALLOWED_HOSTS`, and `CI_WEBHOOK_SIGNING_SECRET`. Redis URLs must use `rediss://`; deployable Kafka must use `SASL_SSL` credentials.

## Non-destructive CI evidence

The CI workflow runs migrations, `verify_supabase`, a pgvector/tenant-backfill check, strict OpenAPI validation, and the PostgreSQL test suite. Preserve the successful workflow URL, commit SHA, timestamp, and the JSON emitted by `verify_supabase` in the release record.

`verify_supabase` fails unless all migration leaves are applied, the PostgreSQL `vector` extension and `knowledge_chunk.embedding` vector column exist, legacy tenant-owned rows have no null organization, and the Supabase Data API is locked out of Django's tables (no `anon`/`authenticated` grants in `public`, RLS enabled on every table).

## Supabase as the identity source of truth

- Tokens are verified against the project's JWKS (`SUPABASE_JWKS_URL`); `SUPABASE_JWT_SECRET` must be unset in staging/production. Only `role=authenticated` sessions are accepted, and anonymous sign-ins are rejected unless `SUPABASE_ALLOW_ANONYMOUS_USERS=true`.
- `SUPABASE_SECRET_KEY` (server-only) is used for the Auth Admin API. Account deletion deletes the Supabase Auth user first and only then anonymizes local data.
- Configure a Supabase **Database Webhook** on `auth.users` (INSERT/UPDATE/DELETE) to `POST https://<api>/api/v1/webhooks/supabase/` with header `Authorization: Bearer <SUPABASE_WEBHOOK_SIGNING_SECRET>`. Bans (`banned_until`) and soft deletes (`deleted_at`) deactivate the local user.
- Django is the only data path: migration `governance.0011` revokes the Data API roles from the `public` schema. Never add RLS policies or grants that expose Django tables to the publishable key.
- The direct host `db.<ref>.supabase.co` is IPv6-only. Runners without IPv6 (including GitHub-hosted runners) must use the Supavisor **session** pooler URL; use the **transaction** pooler only with `DATABASE_POOLER_MODE=transaction` and `DATABASE_CONN_MAX_AGE=0`.

## Manual restore drill

Run this only after an operator has confirmed the restore URL is disposable and different from the source URL. It uses `pg_dump` and `pg_restore`, so the target database is overwritten.

```bash
export DJANGO_SETTINGS_MODULE=config.settings.staging
export DATABASE_URL='source-isolated-supabase-url'
export RESTORE_DATABASE_URL='different-disposable-restore-url'
python manage.py migrate --noinput
python manage.py verify_supabase --format=json
python manage.py seed_restore_drill_fixture
python manage.py restore_drill_check \
  --restore-database-url "$RESTORE_DATABASE_URL" \
  --confirm-restore-target --require-source-data --format=json
```

Record the drill identifier, source backup/PITR timestamp, target project, start/end time, row-count result, analytics-view access result, RTO/RPO, and any remediation. See `BACKUP_RESTORE_RUNBOOK.md` for the recovery policy.

## Phase 5 worker drill

Use staging Redis and a callback receiver whose hostname is listed in `WEBHOOK_ALLOWED_HOSTS`.

1. Start dedicated Celery workers and Beat using `WORKER_OPERATIONS.md`.
2. Submit a native job with a callback URL and record its job and Celery task IDs.
3. Stop the executing worker, wait past `JOB_STALLED_TIMEOUT_SECONDS`, and verify recovery requeues exactly one unfinished step.
4. Verify the terminal job has one credit settlement, one terminal outbox event, and one signed callback delivery with an idempotency key.
5. Repeat with a cancellation and confirm the resumed delivery cannot change the terminal cancelled state.

Record Redis endpoint identity, worker names, job IDs, callback receiver evidence, and outcome. Do not record callback signing secrets or payloads containing customer data.

## Phase 6 Kafka drill

Start the managed-Kafka consumer after migrations:

```bash
python manage.py run_kafka_consumer integrations.webhook.received \
  --consumer-name integration-webhooks
```

The registered `integrations.webhook.received` consumer verifies the delivery and webhook IDs, transitions the persisted verified inbound delivery from `pending` to `delivered`, and writes its `ConsumedEvent` idempotency record in the same transaction. Run the duplicate, invalid-envelope, and broker-outage scenarios in `EVENT_PLATFORM.md`; retain consumer-group, topic/partition/offset, dead-letter, and outbox evidence in the release record.
