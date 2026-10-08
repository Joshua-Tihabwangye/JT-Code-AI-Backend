# ADR-001: Supabase Auth and Django authorization boundary

- **Status:** Accepted
- **Date:** 2026-09-20
- **Related:** Production backlog Phase 0, Phase 2

## Context

The approved architecture replaces the previous Clerk-based authentication with
Supabase Auth as the identity provider, while keeping business policy and
authorization in Django. Two responsibilities must be cleanly separated:

1. **Authentication** — verify "who you are". Supabase issues and revokes
   session JWTs; Django must never mint user sessions itself.
2. **Authorization** — decide "what you may do". Django owns the tenant,
   membership, role and permission model and enforces it against PostgreSQL.

Asset identity and ownership are Django-owned. See ADR-002 for the Supabase
Storage provider boundary.

## Decision

### Identity and authentication ownership (Supabase)

- Supabase Auth is the sole issuer of user sessions via signed JWTs.
- Django verifies JWTs itself using the Supabase JWKS endpoint (and the legacy
  HMAC secret path for development/testing), validating signature, expiry,
  audience and issuer before trusting any claim.
- The Supabase `id` (subject) is the canonical external key that maps to the
  local `identity.User.supabase_user_id`.
- User lifecycle (create/update/deactivate) is synchronized through the
  HMAC-signed Supabase user webhook; Django never stores passwords or performs
  password login.

### User mapping and authorization ownership (Django)

- Django maintains a local `identity.User` row keyed by the Supabase subject.
- Tenants are `identity.Organization`; membership is `identity.UserOrganization`;
  roles and permissions are Django-owned models (RBAC) implemented in Phase 2.
- Every Django resource that participates in collaboration is tenant-scoped via
  its `organization` foreign key and must be looked up through organization
  membership (see Phase 2 tenant-scoping policy).
- Django, not Supabase, is the source of truth for tenant membership,
  roles, permissions, feature access and resource ownership.

### Boundary rule

- Nothing in the Django request path may trust client-supplied role or
  organization claims from the JWT. JWT claims may only seed the local mapping;
  every authorization decision is made against the local database.
- Secrets: only `SUPABASE_URL`, `SUPABASE_JWT_SECRET`, `SUPABASE_JWT_AUDIENCE`,
  `SUPABASE_JWT_ISSUER` and `SUPABASE_WEBHOOK_SIGNING_SECRET` may be present in
  the environment; no Clerk configuration may remain.

## Consequences

- **Positive:** single identity provider, no shared session state, clean audit
  trail, and Django remains the sole authorization authority.
- **Negative:** Django depends on Supabase availability for new logins; JWKS is
  cached with a TTL and must be monitored.
- **Action:** remove all Clerk packages, env vars, middleware and webhooks
  (already complete in Phase 2); must keep Supabase JWT/JWKS tests covering
  expired tokens, forged tokens, wrong issuer and cross-tenant access.

## Verification

- `apps/identity/authentication.py` + `tests/test_identity_auth.py`.
- No `clerk` references anywhere in the repository (grep must return nothing).
