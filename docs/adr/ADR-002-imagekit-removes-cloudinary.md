# ADR-002: ImageKit asset architecture (removes Cloudinary)

- **Status:** Accepted
- **Date:** 2026-09-20
- **Related:** Production backlog Phase 0, Phase 11

## Context

Assets (uploads, generated images, converted documents, chart artifacts) were
previously stored with Cloudinary. The approved architecture replaces
Cloudinary with ImageKit as the hosted media provider, and ImageKit's identity
and ownership must remain Django-owned.

## Decision

- **ImageKit** is the canonical asset bytes/CDN provider going forward.
- Cloudinary is deprecated and must be removed completely (Phase 11): no
  packages, no configuration, no environment variables, no API code, no
  documentation references.
- The browser never receives the ImageKit private key. Django signs short-lived
 , scoped upload tokens; the completion endpoint verifies the upload
  server-side before persisting asset metadata.
- Django persists asset **metadata, ownership, checksums and provenance** in
  `assets.Asset`; ImageKit stores only bytes and transformations.
- Delivery URLs are signed and access-controlled server-side; lifecycle/delete
  and orphan cleanup jobs run through a Django-owned worker.
- Existing asset references must be migrated in Phase 11 before Cloudinary is
  decommissioned.

## Consequences

- **Positive:** no vendor lock-in and no secret exposure; verifiable
  uploads; full control of asset ACLs and lifecycle.
- **Negative:** migration effort (Phase 11) plus double-run of
  storage/Cloudinary code until removed; CDN outage affects deliverability.
- **Action:** `documents`/`conversions` renderers, `assets` upload views and
  all reported Cloudinary configuration will be re-pointed at ImageKit in
  Phase 11 and the old integration deleted.

## Verification

- After Phase 11: no `cloudinary` module import, env var or docs reference;
  ImageKit integration tests pass (signed upload, server-side verify,
  signed delivery, delete/orphan cleanup).