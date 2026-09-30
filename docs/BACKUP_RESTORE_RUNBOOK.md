# Backup, PITR and Restore Runbook

Phase 3 data-foundation artifact for Supabase PostgreSQL.

## Scope

Canonical data lives in Supabase PostgreSQL: users, organizations, memberships,
conversations, jobs, asset metadata, usage ledger, audit/governance records and
billing state. Redis, Kafka, n8n and Streamlit are not canonical stores.

## Production Baseline

- Production `DATABASE_URL` must use the Supabase direct or session-pooler
  PostgreSQL URI with `sslmode=require` (prefer `verify-full` with
  `DATABASE_SSLROOTCERT` when the deployment trusts a supplied CA).
- Set `DATABASE_POOLER_MODE` to `transaction`, `session`, or `direct`. Transaction
  poolers require `DATABASE_CONN_MAX_AGE=0`; persistent Django connections are
  appropriate only for direct/session poolers.
- Supabase Point-in-Time Recovery must be enabled with at least 24 hours of
  PITR coverage before production launch.
- Logical backups must be scheduled daily for schema verification and
  portability.
- Restore drills target RTO <= 4 hours and RPO <= 1 hour, matching
  `docs/SLOs.md`.

## Restore Drill

1. Pick a restore timestamp inside the PITR window and record the incident or
   drill ID.
2. Restore Supabase PostgreSQL into an isolated project or database instance.
3. Configure a staging API instance with the restored `DATABASE_URL` and
   production-equivalent non-production secrets.
4. Run migrations with `python manage.py migrate --settings=config.settings.staging`.
5. Run `python manage.py verify_supabase --format=json --settings=config.settings.staging`
   and retain the output with the drill record.
6. Run `python manage.py check --deploy --settings=config.settings.staging`.
7. Verify read-only analytics views:
   `analytics_job_summary`, `analytics_usage_ledger`,
   `analytics_billing_summary`, `analytics_conversation_summary`,
   `analytics_asset_summary`.
8. Run tenant-isolation smoke tests against representative users and
   organizations.
9. Execute the automated drill from a machine with PostgreSQL client tools:
   `python manage.py restore_drill_check --restore-database-url "$RESTORE_DATABASE_URL" --confirm-restore-target --settings=config.settings.staging`.
   The target must be an isolated, disposable PostgreSQL database; the command
   refuses to use the source database and compares canonical-table row counts.
10. Record actual RTO/RPO, anomalies and follow-up actions.

## Analytics Access

Migrations provision the no-login group role `jt_code_analytics_reader`, revoke
PUBLIC access to the views, and grant it `SELECT` on the five `analytics_*`
views only. Provision a separate login role for Metabase or Streamlit and grant
it membership in `jt_code_analytics_reader`; do not give those consumers the
Django application role. Application tables remain owned by Django migrations
and service code.

## Rollback

Application migrations must be reviewed before production release. If a schema
change cannot be reversed, the release notes must include a compensating action
and the restore drill must confirm the latest backup can recover the previous
state.
