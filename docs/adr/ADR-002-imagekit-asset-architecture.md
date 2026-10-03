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
  single-use, tenant-bound `UploadIntent`, then signs its short-lived token.
  Completion verifies the exact private file path, size, content type and
  provider identity before registration.
- Django downloads the verified object through a signed URL and stores its
  cryptographic SHA-256 content checksum separately from the provider identity
  fingerprint. Django persists asset **metadata, ownership, checksums and provenance** in
  `assets.Asset`; ImageKit stores only bytes and transformations.
- Delivery URLs are signed and access-controlled server-side; lifecycle/delete
  and orphan cleanup jobs run through a Django-owned worker.
- Unmapped legacy rows are fail-closed as quarantined; all usable rows store a
  verified ImageKit `fileId` and `filePath`.

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
- ImageKit integration tests cover signed upload intents, private-file
  completion, checksum verification, signed delivery, role-protected deletion,
  server-generated registration, reconciliation, and aged orphan cleanup.
