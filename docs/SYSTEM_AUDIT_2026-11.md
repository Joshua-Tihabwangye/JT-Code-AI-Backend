# JT-Code system audit (2026-11-16)

Scope: both repositories (`jt-code-backend` on `Working_256`, `jt-code-frontend` on `Working_256`),
all docs in `docs/` of both, the running local Docker stack, and the native stack's database.
Work items that follow from this audit are in [EXECUTION_BACKLOG.md](EXECUTION_BACKLOG.md).
Agent rules are in [../CLAUDE.md](../CLAUDE.md).

Every claim below was checked against code or a live system; the evidence is named next to it.

---

## 1. The system as built

```
Browser (React 19 SPA, supabase-js, axios)
   │  HTTPS
   ▼
Edge TLS proxy (host Caddy, documented; not part of compose today)
   ├─▶ api.<domain>       → api container (uvicorn ASGI, 2 workers) → Django 5.2 + DRF
   └─▶ supabase.<domain>  → supabase-gateway (nginx) → GoTrue :9999 /auth/v1
                                                      → Storage API :5000 /storage/v1
                                                      → PostgREST :3000 /rest/v1
Django ──▶ supabase-db (PostgreSQL 17 + pgvector), one instance, schemas: public (Django), auth (GoTrue),
       │   storage, viz_data (dashboard query store, read through role jt_viz_reader)
       ├─▶ redis-cache  (allkeys-lru): Django cache, rate limits, circuit breaker, SSE pub/sub, result cache
       ├─▶ redis-broker (noeviction, AOF): Celery transport + results
       ├─▶ Celery: worker (6 queues), worker-documents (LibreOffice), beat (32 schedules, outbox every 2 s)
       ├─▶ PostgreSQL outbox ──beat──▶ Kafka (Redpanda, 1 broker, SCRAM + ACLs) ──▶ consumer (1 topic)
       ├─▶ isolated Python engine children (analytics/visualize zygote, rlimits, audit-hook guard)
       └─▶ external: Gemini/OpenAI/Llama (AI gateway), Stripe, n8n, Sentry, SMTP (Gmail)
Observability: Prometheus, Grafana, OTel collector → Tempo, Redis exporters, Redpanda console, RedisInsight.
```

| Concern | Owner | Evidence |
| --- | --- | --- |
| Identity, passwords, sessions | Supabase GoTrue (`auth.users`, `auth.sessions`) | ADR-001, `compose.yaml` `supabase-auth` |
| Profile, tenancy, roles, every business row | Django (`public.*`) | `apps/identity/models.py` |
| File bytes | Supabase Storage (private bucket, signed URLs) | ADR-002 |
| Vectors | pgvector in the same PostgreSQL | ADR-003 |
| Events | PostgreSQL outbox → Kafka | ADR-005, `docs/EVENT_PLATFORM.md` |
| Background work | Celery over the broker Redis | `config/settings/base.py` |

Two parallel local stacks exist and share ports 8000/54321, so only one runs at a time:

* **Native** (`.env`, `make native-up`): host PostgreSQL 18 on `:5432` (database `jt_code`, its own GoTrue).
* **Docker** (`.env.docker`, `make docker-up`): Supabase PostgreSQL 17 on `:54322`. Running now; the frontend's
  `.env.local` points to it.

They are **different databases with different users**. The same e-mail address has a different Supabase id
in each.

## 2. Root cause: "the account has only an e-mail, no name, no hashed password"

### What the databases contain (checked 2026-11-16)

| Where | Row for `tihabwangyej@gmail.com` |
| --- | --- |
| Docker `auth.users` (`:54322`) | Supabase id `2ae7db42…`, bcrypt hash (60 chars, `$2a$`), `raw_user_meta_data` with first/last name, contact, country, timezone. Created 2026-11-13 through the sign-up page. |
| Docker `public.identity_user` | **Complete:** Joshua Walker, contact, country UG, timezone. Workspace "Joshua's workspace". |
| Native `public.identity_user` (`:5432/jt_code`) | Supabase id `25fa4dc1…`, **only the e-mail**; `first_name`, `last_name`, `full_name`, `contact`, `country`, `timezone` and `password` are empty. Created 2026-10-12 20:15:48 together with an organization called **"JT-Code"**. |

### Why the native row is empty

1. The row was not created by the sign-up page. It was created by
   `python manage.py create_local_account tihabwangyej@gmail.com --org "JT-Code"`. The organization name
   proves it: sign-in provisioning would have named it "<first name>'s workspace".
2. That command (`apps/identity/management/commands/create_local_account.py`) creates the Supabase user
   through the Admin API with **only `email` + `password`, no `user_metadata`**. It then creates the Django
   row with `defaults={"email": email}`. No code path in that command collects a name.
3. On every later sign-in, `SupabaseJWTAuthentication.authenticate` finds the existing row
   (`created=False`). `provision_on_authentication` calls `apply_signup_metadata` **only when
   `created` is true** (`apps/identity/provisioning.py`). An existing row is never filled, even if
   metadata exists in Supabase later.
4. **The empty password is by design**, not data loss. Supabase owns credentials (ADR-001). The bcrypt hash
   lives in `auth.users.encrypted_password`. Django's `password` column is unused, but it is stored as `''`
   instead of an explicit "unusable password" marker, which makes the row look broken.

### Latent defects on the same path (fixed in Phase 1)

* **Supabase user webhook** (`apps/identity/webhooks.py`, used once a Database Webhook on `auth.users` is
  configured in production) reads `record["user_metadata"]`. A Database Webhook sends the column
  `raw_user_meta_data`, so the name is always empty. On **every UPDATE it overwrites** `full_name`,
  `display_name` and `avatar_url` with those empty values. GoTrue updates `auth.users.last_sign_in_at` on
  every sign-in, so each sign-in would erase profile edits. It also creates the row first, so the next
  sign-in sees `created=False` and never applies the metadata: the same "only e-mail" symptom.
* `PATCH /accounts/me/` updates `full_name` only. `first_name`/`last_name` go stale. Docker rows already
  show "Ada Integration" vs full name "Ada Lovelace".
* The sign-up form requires accepting the terms, but nothing records it. `ConsentRecord` stays empty and
  `/accounts/me/` reports `termsAccepted: false`.

## 3. Frontend ↔ backend connection map

The frontend has one switch: `VITE_DATA_MODE=api|mock` (`src/lib/config.ts`). In `api` mode every
repository in `src/lib/data/api/repositories.ts` calls Django through `src/lib/api/http.ts`. That client
adds the Supabase access token and `X-JT-Code-Client`. Auth calls go to GoTrue through supabase-js.

**Contract status:** the frontend makes **157** HTTP calls (2 more than the stored snapshot:
`/visualize/datasets/{id}/prediction-options/` and `/visualize/datasets/{id}/questions/`). Every call
resolves to a backend operation. Evidence: regenerated `tests/fixtures/frontend_api_contract.json`, and
`pytest tests/test_phase18_verification.py -k contract` passes.

| Frontend area (route) | Backend app / endpoints | State |
| --- | --- | --- |
| Sign-up / sign-in / reset / SSO (`/sign-*`, `/forgot-password`, `/sso-callback`) | GoTrue `/auth/v1/*`; Django provisions on first call | Connected; profile sync bug (section 2) |
| Chat (`/app/chat`) | `conversations` (`/conversations/`, `/chat/…` SSE), `jobs`, `ai_gateway`, `knowledge` | Connected |
| History (`/app/history`) | `conversations` list/search/pin/archive/export/bulk delete | Connected |
| Images (`/app/images`) | `ai_gateway` image views (`/images/*`), `assets` | Connected (provider key needed for real output) |
| Visualize + dashboards (`/app/visualize/*`, `/share/:t`, `/embed/:t`) | `visualize` (≈60 calls), `analytics`, `documents` | Connected |
| Billing (`/app/billing`) | `billing`, `usage` (plans, wallet, usage, invoices, subscriptions, payment methods) | Connected; needs real Stripe keys |
| Settings (`/app/settings`) | `identity` (`/accounts/me/*`, `/settings/*`), `assets` (avatar), `integrations` | Connected |
| Files / documents / knowledge (inside chat and settings) | `assets` `/files/*`, `documents`, `knowledge` | Connected |

**Backend features with no screen:**
* team management: organization members, invites and roles (`/organizations/members/*`,
  `/organizations/invites/*`);
* agents and agent runs; automations; API keys;
* MCP servers, tools and approvals;
* prompts and policies;
* support cases; outbound webhooks; audit log; retention.

They are API-complete and tested in pytest only. Team management is the one a multi-user product needs
first (backlog Phase 7).

## 4. Database topology

* **One PostgreSQL instance, scaled vertically.** No read replicas, sharding or partitioning
  (ADR-006: partition only on measured need; `capacity_audit` found no candidate). Nothing is "parallel" or
  "horizontal" today. Every service (Django, GoTrue, Storage, PostgREST) shares one database, `postgres`, in
  separate schemas.
* **Connections.** `DATABASE_POOLER_MODE=direct`. Under ASGI (`config/asgi.py`), `CONN_MAX_AGE` is forced to
  0, so **every API request opens and closes a database connection** (TCP + SCRAM auth). Workers keep
  connections for 60 s. There is no pooler between the app and PostgreSQL in compose. All session-level SQL
  in the code is transaction-scoped (`SET LOCAL`, `set_config(..., true)`, `pg_advisory_xact_lock`).
  Django's psycopg3 backend uses client-side binding, so a transaction-mode PgBouncer is safe (Phase 5).
* **Roles.** The app connects as `postgres`: `rolsuper=false`, but **`BYPASSRLS` and `CREATEROLE`**, with
  read access to `auth.users`, which holds the password hashes and refresh tokens. The Data API lockdown
  (`governance.0011`) is effective: `anon`/`authenticated` hold no grants on `public`, and RLS is on in
  109/109 tables (checked). It does not protect against the app role itself (Phase 5 adds a runtime role).
* **Backups.** A nightly `pg_dump` cron line is documented only. Nothing in the stack runs it (Phase 8).

## 5. Load balancing, level by level

| Level | Today | Single-host plan (no Kubernetes) |
| --- | --- | --- |
| Edge / TLS | Host Caddy, documented but not in compose. The frontend container is not part of any stack. | `edge` Caddy container: automatic HTTPS, serves the SPA, proxies `/api` and Supabase, blocks internal paths (Phase 4) |
| API | 1 container × 2 uvicorn workers, kernel-balanced on one socket | `API_REPLICAS` containers behind Caddy (`dynamic a` upstreams, `least_conn`, active health checks on `/api/v1/health/ready/`) |
| Auth (GoTrue) | 1 container behind the nginx gateway; stateless (state in PostgreSQL), so it can be replicated | Per-IP rate limits fixed first (Phase 3). Replicas are optional. |
| Celery | 1 worker container (concurrency 4, 6 queues) + 1 documents worker | `docker compose up --scale worker=N`; queue-specific workers via `CELERY_QUEUES` |
| Kafka | 1 broker; partitions per topic from `KAFKA_TOPIC_PARTITIONS` | Partitions are the scaling unit; consumer replicas ≤ partitions |
| Redis | 1 cache + 1 broker | Vertical; split further only on measured need |
| Database | 1 instance | PgBouncer (Phase 5); vertical size; a read replica for analytics only when measured |

## 6. Caching, layer by layer

| Layer | What is cached | Where |
| --- | --- | --- |
| Browser | Hashed Vite assets (`/assets/*`) | Not set today: the frontend nginx sends no cache headers (Phase 4) |
| Edge | Nothing (correct for an authenticated API) | Caddy `encode zstd gzip`; immutable SPA assets |
| Django | Workspace-provisioning check (1 h), JWKS (1 h, in process), rate-limit counters, AI circuit-breaker state, webhook nonces, n8n registry, visualize/analytics results keyed by op + request + data SHA, dashboard query results | `redis-cache` (`KEY_PREFIX jt-code:cache`, `jt-code:rate-limit`) |
| Per request | Organization and role lookups repeat on every call (2–6 queries) | Phase 6: memoize on the request |
| Database | Shared buffers only | — |
| Auth responses | `Cache-Control: no-store` on authenticated responses | `apps/core/edge.py` (correct) |

## 7. Event bus and asynchronous paths

1. **Transactional outbox.** A state change writes `events.OutboxEvent` in the same transaction.
   `publish_outbox_batch` runs every 2 s from beat, claims a lease, produces up to 500 events, and retries
   with backoff. The result is at-least-once delivery. Published rows are pruned after 7 days.
2. **Kafka.** Topics are `<KAFKA_TOPIC_PREFIX>.<event_type>`, provisioned by `migrate` (no
   auto-create). Envelope v1 carries `request_id`/`trace_id`. One consumer group is deployed
   (`integrations.webhook.received`), with an idempotency ledger (`ConsumedEvent`), a DLQ
   (`DeadLetterEvent`) and replay by command. All other topics are a durable record for future consumers.
3. **Celery.** Durable job state lives in PostgreSQL (`jobs.Job`, `ChatRequest`, …). Beat sweeps re-dispatch
   stalled or queued work, so losing the broker loses no work.
4. **Redis pub/sub.** Chat status for SSE streams, with a database reconciliation fallback.
5. **n8n** (external): signed dispatches and nonce-bound callbacks.

## 8. Request lifecycle (API)

Middleware runs in this order:
1. `RequestContext`: request/trace id.
2. `Metrics`.
3. `EdgeProtection`: client IP and the optional Cloudflare origin lock.
4. `ReadOnlyMode`.
5. `Security` and `SecurityHeaders`: CSP and `no-store`.
6. WhiteNoise, CORS, sessions, CSRF, auth.
7. `AuditTrail`.

DRF then runs `SupabaseJWTAuthentication`:
1. Verify HS256 or JWKS and require `role=authenticated`.
2. `get_or_create` the user.
3. Provision (cached hourly).

Next comes `IPRateThrottle` (300/min). Views add per-feature throttles, tenant resolution
(`X-Organization-ID` or the primary organization, membership checked), role checks and the usage
reservation (credits).

## 9. Security findings and penetration surface

Severity: **H** high, **M** medium, **L** low. Status as of this audit.

| # | Sev | Finding | Evidence | Phase |
| --- | --- | --- | --- | --- |
| F1 | H | **Frontend production image runs in mock mode.** The Dockerfile passes no `VITE_DATA_MODE`/`VITE_SUPABASE_*` (it still passes the removed Clerk key). A built image signs users in against `localStorage`, with no backend. Its nginx proxies to a non-existent host `jt-code-api`. | `jt-code-frontend/Dockerfile`, `nginx.conf`, `src/lib/config.ts` | 4 |
| F2 | H | **App database role can read `auth` (password hashes, refresh tokens) and has BYPASSRLS/CREATEROLE.** A SQL injection or an analytics-sandbox escape becomes full identity compromise. | `pg_roles`, `compose.yaml` `DATABASE_URL` | 5 |
| F3 | H | **GoTrue rate limits are one global bucket.** GoTrue sees every request from the gateway's IP (no `GOTRUE_RATE_LIMIT_HEADER`). One attacker can use up the e-mail/OTP/sign-in budget for all users (auth DoS), and per-IP brute-force limits do not exist. | `compose.yaml` `supabase-auth` | 3 |
| F4 | H | **No token revocation.** Sign-out, ban or deletion in Supabase does not stop Django from accepting the access token until it expires (`JWT_EXP` 3600 s). | `apps/identity/authentication.py` | 2 |
| F5 | M | **Client IP spoofing.** uvicorn runs `--proxy-headers --forwarded-allow-ips *`, and uvicorn 0.54 then takes the *leftmost* (client-supplied) `X-Forwarded-For` as the peer. Wherever `TRUSTED_PROXY_HOPS=0` (local/dev, or a missed setting), per-IP throttles and audit IPs can be forged. | `docker/entrypoint.sh`, `uvicorn/middleware/proxy_headers.py:176` | 2 |
| F6 | M | **Server password policy weaker than the UI:** GoTrue defaults (min 6, no character classes) vs UI (8, letter + digit). Direct `/auth/v1/signup` calls bypass it. | `compose.yaml`, `signup/schema.ts` | 3 |
| F7 | M | **Unneeded public surface on the Supabase gateway:** `/rest/v1` (PostgREST; the SPA never uses it) and `/auth/v1/admin/*` are routed publicly. They are locked down today, but one wrong grant or a leaked service key becomes reachable from the internet. | `docker/supabase/nginx.conf` | 3 |
| F8 | M | **No CSP/security headers on the SPA.** supabase-js stores the session (access + refresh token) in `localStorage`, so an XSS is an account takeover. CSP is the main mitigation. | `jt-code-frontend/nginx.conf` | 4 |
| F9 | M | **Supabase webhook profile overwrite** (section 2). | `apps/identity/webhooks.py` | 1 |
| F10 | M | **Gateway CORS `*`** lets any website drive sign-up, recovery and OTP endpoints from visitors' browsers (e-mail bombing, sign-up spam). | `docker/supabase/nginx.conf` | 3 |
| F11 | L | **Invite tokens stored in plaintext** (bearer secrets in the DB). Mitigated by the e-mail match on accept. | `identity.OrganizationInvite` | 2 |
| F12 | L | **Django admin on the public API host.** `AUTH_PASSWORD_VALIDATORS` is empty. No staff users exist today. | `config/urls.py`, `base.py` | 2/4 |
| F13 | L | **Sign-up abuse:** no CAPTCHA; every confirmed sign-up gets a workspace and free credits. | `provisioning.py` | 3 (option) |
| F14 | L | **Consent not recorded** at sign-up (compliance). | section 2 | 1 |
| F15 | M | **New database connection per API request** (ASGI, no pooler): latency and connection storms under load. | `config/settings/base.py` | 5 |
| F16 | M | **Backups not automated;** one host means every service is a single point of failure. | `SELF_HOSTED_DEPLOYMENT.md` | 8 |

Already in place (verified, keep): fail-closed settings validation; JWT `role=authenticated` and anonymous
rejection; tenant-scoped querysets with cross-tenant tests; Data API lockdown and RLS; signed and
replay-protected webhooks (Stripe/n8n/inbound); SSRF-safe egress with DNS pinning; Python sandbox with
rlimits and audit hooks; private storage with SHA-256 verification; append-only audit/ledger triggers;
`no-store` on authenticated responses; non-root, digest-pinned image; loopback-only ports; Kafka SCRAM +
ACLs; Redis passwords.

### Penetration surface (what an attacker can reach)

| Surface | Reachable by | Main risks | Control (✓ in place, → planned) |
| --- | --- | --- | --- |
| `https://<app>/` SPA | anyone | XSS → token theft | → CSP + headers (P4) |
| `/api/v1/*` (293 paths) | authenticated users | IDOR/cross-tenant, mass assignment, abuse of credits | ✓ tenant scoping + tests, throttles; → revocation (P2), IP fix (P2) |
| Anonymous API: health, `visualize/share/*` + embeds, Stripe webhook, inbound webhooks, n8n callbacks, Supabase webhook, plans catalog | anyone | token guessing, replay, forged webhooks | ✓ signatures/nonces, share tokens + field allowlist, throttles |
| `/auth/v1/*` GoTrue | anyone | credential stuffing, e-mail bombing, weak passwords, sign-up spam | → per-IP limits, password policy, CORS allowlist, edge rate limits (P3) |
| `/auth/v1/admin/*`, `/rest/v1/*` | anyone (today) | service-key misuse, grant mistakes | → blocked at the public edge (P3/P4) |
| `/storage/v1/*` | signed URL holders | URL leakage | ✓ short TTL signed URLs, private bucket |
| `/admin/`, `/api/docs/`, `/metrics` | anyone (today, via API host) | admin brute force, schema recon | ✓ docs staff-only, metrics token; → blocked at the edge (P4) |
| Python sandbox, document parsers (PDF/DOCX/OCR/LibreOffice) | authenticated users | RCE / parser exploits | ✓ isolated child, empty env, rlimits, guard; → least-privilege DB role (P5) |
| Tool gateway / knowledge URL fetch | authenticated users | SSRF | ✓ egress layer, DNS pinning, deny list |
| RAG / agents | authenticated users | prompt injection | ✓ taint, approvals, safety gate |

## 10. Deployment readiness (Docker, single server, no Kubernetes)

Ready: production `compose.yaml` with fail-closed preflight (`make prod-check`), health checks on every
service, ordered start-up through `migrate`, loopback-only ports, monitoring stack and logging limits.

Blocking or missing (all scheduled in the backlog):
1. The frontend is not deployable (F1). No compose service serves it.
2. No TLS edge in compose. TLS depends on a hand-installed host proxy.
3. GoTrue hardening (F3, F6, F10).
4. A runtime database role and a pooler (F2, F15).
5. Automated, off-host backups (F16).
6. Real provider keys: Gemini, Stripe (live), SMTP, Sentry, n8n. The operator supplies them.

## 11. Everything that remains to finish the system

Engineering (in the backlog):
* profile sync;
* auth hardening;
* GoTrue/gateway hardening;
* edge, SPA container and API replicas;
* PgBouncer and the runtime DB role;
* per-request caching;
* team management UI and consent recording;
* backups and deploy runbook.

Product or operator decisions (outside the backlog's engineering scope):
* real AI/Stripe/SMTP/Sentry/n8n keys;
* OAuth providers (Google/Apple) in GoTrue;
* whether conversations stay organization-shared or become owner-private;
* chat attachment content injection (a registry only today);
* MFA enrolment UI;
* CAPTCHA provider;
* UIs for agents, automations, API keys, MCP, prompts/policies, audit log, support;
* load-test evidence on the real server (`loadtests/*.json`, `capacity_plan`);
* DR drill on the real server.
