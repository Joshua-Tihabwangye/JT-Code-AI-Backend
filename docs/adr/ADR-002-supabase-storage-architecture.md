# ADR-002: Supabase Storage asset architecture

- **Status:** Accepted
- **Date:** 2026-10-08
- **Decision owners:** Platform / Backend

## Context

JT-Code needs private, low-latency storage for user uploads and server-generated
images, documents, conversion results, analytics artifacts, and knowledge-source
files. Asset ownership, tenant membership, lifecycle state, and audit references
already belong to Django and Supabase PostgreSQL. Keeping object bytes in the
same Supabase project reduces the number of storage providers and removes the
former external asset-provider dependency.

## Decision

- **Supabase Storage** is the canonical provider of asset bytes. The
  `SUPABASE_STORAGE_BUCKET` bucket is private; it is never used as a public CDN
  bucket.
- The server-only Supabase service-role key is supplied through
  `SUPABASE_SECRET_KEY`. It is used only by Django/Celery to create or repair
  the bucket, write server-generated objects, issue signed upload URLs, issue
  signed delivery URLs, and perform lifecycle cleanup. It is never sent to a
  browser or mobile client.
- Every object key starts with
  `SUPABASE_STORAGE_PREFIX/<organization-id>/...`. Django creates an
  unguessable, one-object signed upload capability and persists a single-use,
  tenant-bound `UploadIntent`. Completion accepts only the exact stored key,
  re-downloads it through a short-lived signed URL, verifies byte count, magic
  bytes, and SHA-256 before creating an `Asset` row.
- Django remains the authorization policy decision point. It checks ownership
  and organization membership before issuing a short-lived read URL or
  streaming an object. Supabase Storage is the private byte store, not the
  tenant authorization source.
- Server-generated objects are written with `x-upsert: false` and randomized
  filenames. Reconciliation re-downloads bounded batches and quarantines a row
  if the object is missing, changes size, or no longer matches its SHA-256.
  Soft deletion, retry-capped physical deletion, and aged-orphan sweeping are
  retained.
- The deployment bucket is created/configured with `public=false`, the allowed
  asset MIME types, and the configured byte cap. `manage.py check` validates
  the required Supabase configuration in staging and production.

## Consequences

### Positive

- One managed Supabase boundary now covers Auth, PostgreSQL/pgvector, and
  private asset bytes, reducing operational integration points and asset
  retrieval latency.
- Clients receive expiring, object-scoped upload/read capabilities rather than
  permanent credentials or a public bucket URL.
- The asset registry retains application-level tenant isolation, integrity
  verification, audit events, lifecycle controls, and provider independence.

### Trade-offs / safeguards

- Direct upload clients must use the returned `PUT` URL with the returned
  `x-signature` upload token, then call `/files/complete/` with `storageKey`.
  This supersedes the predecessor upload response shape.
- Existing provider bytes cannot be copied merely by renaming database fields.
  Migration `assets.0010` safely quarantines pre-cutover records. Copy retained
  bytes into the private bucket through the approved transfer process, then run
  `python manage.py migrate_legacy_assets --map <asset-id>=<storage-key>`; the
  command re-verifies the object before returning the row to `ready`.
- A service-role key bypasses Storage RLS. It is therefore secret-managed only,
  never included in client configuration, and every server method applies
  Django authorization first.

## Verification

- `tests/test_supabase_storage_assets.py` covers scoped upload and completion.
- `tests/test_phase11_supabase_storage.py` covers ACL, integrity, fingerprint,
  and lifecycle behavior.
- Run `python manage.py check --settings=config.settings.production` with the
  deployment environment and perform the signed-upload smoke test before
  production promotion.
