# ADR-002: ImageKit asset architecture (removes Cloudinary)

- **Status:** Accepted
- **Date:** 2026-09-20
- **Related:** Production backlog Phase 0

## Context

Assets (uploads, generated images, converted documents, chart artifacts) were
previously stored with Cloudinary. The approved architecture replaces
Cloudinary with ImageKit as the hosted media provider, and ImageKit's identity
and ownership must remain Django-owned.

## Decision

- **ImageKit** is the canonical asset bytes/CDN provider going forward.
- The previous storage provider has been removed from runtime packages,
  configuration, environment variables and API code.
- The browser never receives the ImageKit private key. Django signs short-lived
 , scoped upload tokens; the completion endpoint verifies the upload
  server-side before persisting asset metadata.
- Django persists asset **metadata, ownership, checksums and provenance** in
  `assets.Asset`; ImageKit stores only bytes and transformations.
- Delivery URLs are signed and access-controlled server-side; lifecycle/delete
  and orphan cleanup jobs run through a Django-owned worker.
- Existing asset rows keep their provider identifier through a Django field
  rename migration; new rows store ImageKit `fileId` and `filePath`.

## Consequences

- **Positive:** no vendor lock-in and no secret exposure; verifiable
  uploads; full control of asset ACLs and lifecycle.
- **Negative:** CDN outage affects deliverability; historical migrations retain
  legacy field names so old databases can migrate forward safely.
- **Action:** lifecycle/delete and orphan cleanup jobs should be expanded as
  asset governance hardens.

## Verification

- No runtime `cloudinary` package import, dependency, environment variable or
  API code remains.
- ImageKit integration tests pass for signed upload auth, server-side verify
  and generated/rendered byte upload fallback.
