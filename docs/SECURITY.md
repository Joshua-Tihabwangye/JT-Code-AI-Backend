# Security: Data Classification, Threat Model and Initial SLOs

Source: Production Backlog Phase 0 — "Define data classification, threat model
and initial SLOs". This document is the frozen baseline; it is reviewed and
revised before production launch.

## 1. Data classification

| Tier | Definition | Examples | Storage | Access |
|------|-----------|----------|---------|--------|
| **C0 — Public** | Non-sensitive, non-user data | model catalog, plan catalog, this repo (docs exclude secrets) | any | public |
| **C1 — Internal** | Operational, non-personal data | audit categories, usage aggregates, feature flags | PostgreSQL (Django) | authenticated staff |
| **C2 — Confidential** | Personal + business data under access control | user profiles, org membership, conversations, knowledge documents, jobs, assets metadata, billing, consents | PostgreSQL; assets via signed private Supabase Storage delivery (ADR-002) | members of owning organization only |
| **C3 — Restricted** | Highest sensitivity/regulated | credentials (API keys), webhook secrets, Supabase/Stripe keys, SafetyEvent containing content, export payloads at-rest | secrets only in env/vault; never in logs or source | service accounts, minimal staff, audit-logged |

**Rule of thumb:** any row carrying a user or organization reference is at least
C2 and must be returned only through an organization-membership authorization
path (see ADR-001 and Phase 2 RBAC). This is a production invariant; until
Phase 2 completes, this document records the required control rather than
claiming the invariant is already enforced.

## 2. Threat model

Trust boundaries: Client → Django (JWT), Django → Supabase (JWKS/webhook),
Django → PostgreSQL, Django → services (Redis/Celery/Kafka/n8n/Stripe/Sentry/providers).

| Threat | Vector | Required production control / delivery gate |
|--------|--------|----------------------------------------|
| T01 Forged/expired session | Fake JWT over the API | Fail-closed JWKS verification, expiry and leeway; Phase 2 tests for expired/garbage tokens |
| T02 Wrong issuer/audience accepted | Token issued by a different Supabase project | Issuer/audience validation with a Phase 2 issuer-negative test |
| T03 Cross-tenant data access | Predictable UUIDs, missing org filter | Organization-scoped querysets and vector-store re-check; Phase 2 endpoint-level cross-tenant test suite |
| T04 Privilege escalation | Insufficient RBAC | Phase 2 Role/Permission models, seeded roles and authorization enforcement |
| T05 Secrets in source/logs or client | Misconfigured `.env`, debug logs | Phase 1 placeholder-only `.env.example`, gated secret/dependency scanning and body-free structured logs |
| T06 Webhook forgery | Fake Supabase/n8n/Stripe callbacks | Phase 1–2 HMAC/signature verification for Supabase, Stripe and n8n callbacks |
| T07 Prompt injection via RAG/docs | Injected instructions in ingested content | Phase 10 tool allowlist and LLM gating; record safety events |
| T08 Supply chain | Malicious dependency | Phase 1 gated dependency scanning and reviewed version ranges |
| T09 Exposure of health/telemetry internals | `/api/v1/health/ready/` returns internals | Phase 1 readiness reports only ok/failed per check, never stack traces |

Data-loss/DR concerns are covered by the Phase 3 backup/PITR procedures.

## 3. Initial SLOs (target: pre-100K users)

| Metric | Initial target |
|--------|----------------|
| API availability (liveness) | 99.9% monthly |
| Ready (DB+Redis) | 99.5% monthly |
| p95 API latency (non-streaming) | ≤ 800 ms |
| p99 API latency (non-streaming) | ≤ 2 s |
| Chat completion p95 (gateway path) | ≤ 15 s |
| Event outbox drain (P95 age to publish) | ≤ 5 s |
| Job completion (P95, standard jobs) | ≤ 10 min |
| Recovery time objective (RTO) | 4 h |
| Recovery point objective (RPO) | 1 h (PITR) |

Measurement baseline: Sentry traces + `/api/v1/health/*` probes; dashboards and
OpenTelemetry come in Phase 15.

## 4. Hardening controls (Phase 15)

The controls behind the threat model are documented in
[OBSERVABILITY.md](OBSERVABILITY.md):

* Sentry PII scrubbing.
* Authenticated metrics.
* Tracing with credential scrubbing at the collector.
* The append-only audit pipeline, exported through Kafka.
* Security headers and a strict API CSP.
* Cloudflare WAF, edge rate limits and the origin lock.
* Trusted-proxy client-IP resolution.
* Timestamped, nonce-bound webhook signatures with replay rejection.
* The SAST, dependency, secret and DAST gates (`manage.py security_gate` and CI).
