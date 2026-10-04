# ADR-002: ImageKit asset architecture

- **Status:** Accepted
- **Date:** 2026-09-20
- **Related:** Production backlog Phase 0

## Context

Assets include uploads, generated images, converted documents, and chart
artifacts. ImageKit is the hosted media provider, while identity and ownership
remain Django-owned.

## Decision

- **ImageKit** is the canonical asset bytes/CDN provider going forward.
- The previous storage provider has been removed from runtime packages,
  configuration, environment variables and API code.
- The browser never receives the ImageKit private key. Django creates a
  single-use, tenant-bound `UploadIntent` and issues an ImageKit **V2 upload
  JWT** whose payload binds the folder (`/<root>/<org>/uploads/<user>`), file
  name, `isPrivateFile=true`, `overwriteFile=false` and an exact size check, so
  the client cannot redirect or widen the upload. Completion verifies the exact
  private file path, size, provider identity and the file's magic bytes
  against the declared content type before registration.
- Clients may instead upload through `POST /api/v1/files/` (multipart); Django
  checks size, allow-listed type (`ASSET_ALLOWED_CONTENT_TYPES`, no SVG) and
  magic bytes, then stores the file privately in ImageKit.
- Every server-side upload (generated images, rendered documents, conversions,
  analytics results and charts) is a private, uniquely named file in the
  tenant folder `/<root>/<org>/<kind>`; nothing is ever overwritten in place.
- Assets are `private` (owner + organization admins) by default and may be
  shared `organization`-wide by their owner/an admin. Consumers (knowledge,
  analytics, documents) expose derived content under their own ACLs, so a
  private upload backing a restricted document is never readable through the
  asset API. Assets belong to the organization: removing a user keeps them.
- Django downloads the verified object through a signed URL and stores its
  cryptographic SHA-256 content checksum separately from the provider identity
  fingerprint. Django persists asset **metadata, ownership, checksums and provenance** in
  `assets.Asset`; ImageKit stores only bytes and transformations.
- Delivery URLs are signed and access-controlled server-side, or bytes are
  streamed through `GET /api/v1/files/{id}/download/`. Deletion is soft (with
  `restore` during `ASSET_DELETE_GRACE_DAYS`) and refuses assets still used by
  a knowledge source, dataset, document, conversion, image or conversation
  unless forced; provider deletion retries up to `ASSET_DELETE_MAX_ATTEMPTS`.
- The provider fingerprint covers `fileId`, `filePath`, `size` and
  `versionInfo.id` (never mutable metadata such as tags or `updatedAt`).
  Reconciliation verifies READY assets in batches of
  `IMAGEKIT_RECONCILE_BATCH_SIZE`, each at most every
  `IMAGEKIT_RECONCILE_INTERVAL_HOURS`; the daily orphan sweep walks the folder
  tree recursively (`IMAGEKIT_RECONCILE_MAX_DEPTH`).
- Unmapped legacy rows are fail-closed as quarantined until
  `manage.py migrate_legacy_assets --map <asset>=<fileId>` verifies and maps
  them; `--local-fallbacks` moves development fallback files into ImageKit.

## Consequences

- **Positive:** no vendor lock-in and no secret exposure; verifiable
  uploads; full control of asset ACLs and lifecycle.
- **Negative:** CDN outage affects deliverability; unmapped legacy asset rows
  are quarantined until an explicit ImageKit mapping is supplied.
- **Action:** monitor the scheduled purge/reconciliation jobs and ImageKit API
  error rate; quarantine events require operator review.

## Verification

- No retired media-provider package, dependency, environment variable, API
  code, documentation, or migration identifier remains.
- `tests/test_imagekit_assets.py` and `tests/test_phase11_imagekit_assets.py`
  cover the V2 JWT parameter binding, private/unique server uploads, magic-byte
  verification, signed delivery, the visibility policy, reference-aware
  deletion and restore, batched reconciliation, recursive orphan sweeps and
  legacy mapping. ImageKit HTTP calls are stubbed; validate against a real
  account with a dedicated ImageKit folder before release.
