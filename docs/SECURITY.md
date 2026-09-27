# Security: Data Classification, Threat Model and Initial SLOs

Source: Production Backlog Phase 0 — "Define data classification, threat model
and initial SLOs". This document is the frozen baseline; it is reviewed and
revised before production launch.

## 1. Data classification

| Tier | Definition | Examples | Storage | Access |
|------|-----------|----------|---------|--------|
| **C0 — Public** | Non-sensitive, non-user data | model catalog, plan catalog, this repo (docs exclude secrets) | any | public |
| **C1 — Internal** | Operational, non-personal data | audit categories, usage aggregates, feature flags | PostgreSQL (Django) | authenticated staff |
| **C2 — Confidential** | Personal + business data under access control | user profiles, org membership, conversations, knowledge documents, jobs, assets metadata, billing, consents | PostgreSQL; assets via signed ImageKit delivery (ADR-002) | members of owning organization only |
| **C3 — Restricted** | Highest sensitivity/regulated | credentials (API keys), webhook secrets, Supabase/Stripe keys, SafetyEvent containing content, export payloads at-rest | secrets only in env/vault; never in logs or source | service accounts, minimal staff, audit-logged |

**Rule of thumb:** any row carrying a user or organization reference is at least
C2 and must be returned only through an organization-membership or ownership
authorization path (see ADR-001 and Phase 2 RBAC).

## 2. Threat model

Trust boundaries: Client → Django (JWT), Django → Supabase (JWKS/webhook),
Django → PostgreSQL, Django → services (Redis/Celery/Kafka/n8n/Stripe/Sentry/providers).

| Threat | Vector | Mitigations (implemented / Phase 2–3) |
|--------|--------|----------------------------------------|
| T01 Forged/expired session | Fake JWT over the API | JWKS + HMAC verification, expiry, leeway; tests for expired/garbage tokens |
| T02 Wrong issuer/audience accepted | Token issued by a different Supabase project | `SUPABASE_JWT_ISSUER`/`AUDIENCE` validation + Phase 2 issuer tests |
| T03 Cross-tenant data access | Predictable UUIDs, missing org filter | tenant-scoped querysets + vector-store org re-check; cross-tenant test suite |
| T04 Privilege escalation | Insufficient RBAC | Phase 2 Role/Permission models + authorization service |
| T05 Secrets in source/logs or client | Misconfigured `.env`, debug logs | `.env.example` placeholders; secret + dependency scanning in CI (Phase 1); structured logs exclude bodies |
| T06 Webhook forgery | Fake Supabase/n8n/Stripe callbacks | HMAC signature checks; Stripe signature verification; signed n8n callbacks |
| T07 Prompt injection via RAG/docs | Injected instructions in ingested content | tool allowlist + LLM-gating (Phase 10); safety events recorded |
| T08 Supply chain | Malicious dependency | dependency scanning in CI (Phase 1), pinned ranges |
| T09 Exposure of health/telemetry internals | `/health/ready` returns internals | readiness reports only ok/failed per check; no stack traces |

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

Measurement baseline: Sentry traces + `/health/*` probes; dashboards and
OpenTelemetry come in Phase 15.
