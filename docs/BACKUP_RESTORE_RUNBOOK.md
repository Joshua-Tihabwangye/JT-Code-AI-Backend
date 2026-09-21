# Backup, PITR and Restore Runbook

Phase 3 data-foundation artifact for Supabase PostgreSQL.

## Scope

Canonical data lives in Supabase PostgreSQL: users, organizations, memberships,
conversations, jobs, asset metadata, usage ledger, audit/governance records and
billing state. Redis, Kafka, n8n and Streamlit are not canonical stores.

## Production Baseline

- Production `DATABASE_URL` must use the Supabase direct or session-pooler
  PostgreSQL URI with `sslmode=require`.
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
5. Run `python manage.py check --deploy --settings=config.settings.staging`.
6. Verify read-only analytics views:
   `analytics_job_summary`, `analytics_usage_ledger`,
   `analytics_billing_summary`, `analytics_conversation_summary`,
   `analytics_asset_summary`.
7. Run tenant-isolation smoke tests against representative users and
   organizations.
8. Compare restored row counts for canonical tables against the source backup
   inventory.
9. Record actual RTO/RPO, anomalies and follow-up actions.

## Analytics Access

Metabase and Streamlit must connect with a read-only PostgreSQL role. They may
query the `analytics_*` views only unless a separate ADR approves broader
access. Application tables remain owned by Django migrations and service code.

## Rollback

Application migrations must be reviewed before production release. If a schema
change cannot be reversed, the release notes must include a compensating action
and the restore drill must confirm the latest backup can recover the previous
state.
