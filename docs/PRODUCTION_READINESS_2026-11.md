# Production readiness (updated 2026-11-26, after Phase 16; Phase 17 audit: [KEYS_ONLY_AUDIT_2026-11.md](KEYS_ONLY_AUDIT_2026-11.md))

The operator's bar for deploying is **95 %**. This report scores each area against evidence gathered on
this machine in Phases 15 and 16 (backlog and progress log: [EXECUTION_BACKLOG.md](EXECUTION_BACKLOG.md)).
It says what was verified, what was not, and what remains.

**Verdict: about 91 %. Still below the 95 % bar.** The engineering a user touches is complete and verified
end to end. That covers every screen, every API operation, and the security probes. It also covers the
whole browser suite on the current dependency versions. What keeps the score below 95 % is outside this
machine:

* operator keys (Gemini, Stripe, SMTP, Sentry);
* a first run on the real server: TLS, off-host backups with a restore drill, and a load test.

Without those the operations score cannot honestly pass about 75 %.

## 1. Evidence (all on the local Docker stack and test database)

| Check | How | Result |
| --- | --- | --- |
| Backend suite | `pytest tests -n 2` on PostgreSQL 18 + pgvector | **1081 passed, 2 skipped, 0 failed** |
| Every API operation, live | `scripts/endpoint_sweep.py` (432 operations, about 990 calls: anonymous, reads with real ids, empty and generated writes, cross-tenant, deletes) | 0 failures in Phase 15. The only 5xx are 5 `503 *_not_configured` (Stripe ×3, Gemini ×2). |
| Attacks from outside, live | `scripts/security_probe.py` | **70/70** (section 3) |
| Frontend ↔ backend contract | `extract_frontend_contract.py`, `pytest -k contract` | **205** frontend calls, each resolves to a backend operation (164 before Phase 16) |
| Frontend repositories against the live stack | `npm run test:integration` | **20/20** (echo AI provider; 17/20 without an AI key) |
| Real browser through the whole UI | `npm run test:e2e` (Chrome, 1366×900) | **35/35 steps**, including the new API keys, webhooks, agents, tools, agreements, audit log and support screens |
| Frontend unit tests, typecheck, lint, build | `npm run check` | 251/251; 0 lint errors; build ok |
| Dependencies | `pip-audit`; `npm audit` (development tools included) | **0 known vulnerabilities** on both sides |
| Static analysis | `bandit -r apps config` | 1 medium, justified (section 3) |
| File descriptors, idle | per-process count while only Docker health probes run | flat at 29 per API worker over 16 probes (before P16-10: steady growth) |
| Monitoring | Prometheus targets, Grafana | all targets up; 5 dashboards; datasource works |

## 2. Score by area

| Area | Score | What holds it back |
| --- | --- | --- |
| Core user features | 96 % | AI output needs `GEMINI_API_KEY`; payments need Stripe keys |
| Frontend ↔ backend integration | 97 % | Every user-facing feature has a screen (section 4). Not built on purpose: prompts. Still without a screen: the generic connector framework |
| Security | 97 % | Section 3. Product decisions left: MFA enrolment UI, a CAPTCHA provider |
| Data and events (PostgreSQL, pgvector, outbox, Kafka, Celery, Redis) | 94 % | Proven here, including the descriptor leak fix; no load test on the real server |
| Operations (deploy, TLS edge, backups, DR, monitoring) | 75 % | The gateway now survives a GoTrue recreate; health probes no longer leak. Never run on the real server: TLS, off-host backups, restore drill, load test |
| Overall (weighted towards what users touch) | **≈ 91 %** | |

## 3. Security

Found and fixed, each with a test that fails without the fix (G9):

| # | Phase | Finding | Fix |
| --- | --- | --- | --- |
| S1 | 15 | `POST /completion/` answered 500 on every call (a credit hold outside a transaction) | one transaction; autocommit tests |
| S2 | 15 | NUL in a query or JSON body reached PostgreSQL: 500 | `NulCharacterGuardMiddleware` (400) |
| S3 | 15 | A forged `X-Organization-ID` was ignored by some endpoints | refused on every endpoint (403) |
| S4 | 15 | Chat errors showed users internal text (key and adapter names, raw exceptions) | user-facing messages per code |
| S5 | 15 | DRF 3.17 unauthenticated DoS; Pillow 12.3 (42 advisories) | DRF 3.18.2, Pillow 13 |
| S6 | 15 | Parallel first requests raced on sign-up consent: 500 | the unique constraint decides |
| S7 | 16 | API keys were issued but never accepted; a revoked key could be reactivated; the digest was returned | `X-API-Key` authentication (scopes, pinned workspace, no account/key management, per-key limit); read-only fields |
| S8 | 16 | Outbound webhooks were never delivered; URLs and headers unvalidated | signed, SSRF-safe, retried delivery; admin-only; validation |
| S9 | 16 | Any editor could assign a support case to **any user id in the system** | customers reply and close only; staff answer in the admin |
| S10 | 16 | Audit filters: malformed values 500; an end date dropped its own day | 400; the whole day; formula-safe CSV export |
| S11 | 16 | Editors could read, change and **delete** other members' consent records | personal, never deleted, terms not withdrawn through the API |
| S12 | 16 | After a GoTrue recreate the gateway answered 502 to all sign-ins until restarted | per-request DNS resolution of upstreams |
| S13 | 16 | **File descriptor leak**: a worker reached 1023/1024 and answered 500; idle growth with every health probe (outage within hours) | the probe closes its broker connection; one Redis pool per process; the chat stream always closes Redis |
| S14 | 16 | 12 npm advisories in development tools (2 critical); zod 3 | zod 4, vitest 5, puppeteer-core 25 |

Verified in place (live): forged, expired, wrong-role, wrong-audience, session-less and tampered tokens get
401. Mass assignment is ignored. Cross-tenant reads, writes and deletes are refused, and so is CORS from a
foreign origin. Security headers are present. The gateway closes `/rest/v1` and `/auth/v1/admin`. GoTrue
enforces the password policy and sign-in rate limits. API keys, webhooks, support cases, audit log,
consents, agent runs and tool credentials stay in their workspace; credential secrets are never returned.

Justified, not fixed: bandit B102 `exec` in `apps/visualize/viz_python.py`, the Python-visual sandbox. It
runs only inside the isolated engine child (empty environment, rlimits, audit-hook guard), after AST
validation.

## 4. Backend features and their screens

Server-to-server or operator-only by design: n8n callbacks, internal endpoints, Kafka consumer
administration, health, Stripe/Supabase/inbound webhooks, monitoring relays, the model catalogue, model
policies, evaluations, safety events.

Built in Phase 16: Settings → Developer (API keys, webhooks), Audit log, Support, Profile → Your agreements;
Agents (agents and runs, scheduled automations, tools: approvals, catalogue, MCP servers, credentials).

Not built:

* prompts, on purpose: nothing in the code reads stored prompts, so a screen would offer a feature that
  does nothing;
* the generic connector framework (`/connectors/`, `/connector-accounts/`). The existing Integrations
  screen uses `/integrations/`. Decide whether connectors are a product feature before building a screen.

## 5. What the operator must do

### Keys and settings (`.env.production` on the server; local tests use `.env.docker`)

| Purpose | Keys |
| --- | --- |
| AI (chat, images, embeddings, agents) | `GEMINI_API_KEY`; optional `GEMINI_DEFAULT_MODEL`, `GEMINI_EMBEDDING_MODEL`; optional fallbacks `OPENAI_API_KEY`, `LLAMA_API_BASE` + `LLAMA_API_KEY` |
| Payments | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PUBLISHABLE_KEY` (live keys in production) |
| E-mail (Auth mail, support notifications) | `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME`; `SUPPORT_NOTIFICATION_EMAIL` (your support inbox) |
| Errors and tracing | `SENTRY_DSN`, `SENTRY_ENVIRONMENT`, `SENTRY_RELEASE`; frontend `VITE_SENTRY_DSN`, `VITE_SENTRY_ENVIRONMENT` |
| Workflows (required: production validation needs `N8N_API_KEY`; n8n is a separate deployment, see [infra/n8n/README.md](../infra/n8n/README.md)) | `N8N_BASE_URL`, `N8N_WEBHOOK_BASE_URL`, `N8N_CALLBACK_BASE_URL`, `N8N_API_KEY`, `N8N_DISPATCH_SECRET`, `N8N_WEBHOOK_SECRET`, `N8N_SENTRY_RELAY_SECRET` (generated), `N8N_CREDENTIAL_*` |
| Webhook automations | `WEBHOOK_ALLOWED_HOSTS`: the hosts automations may call |
| Domains | `APP_DOMAIN`, `FRONTEND_URL`, `SUPABASE_PUBLIC_URL`; frontend `VITE_API_BASE_URL` (full URL on Vercel), `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY` |

New settings with safe defaults: `API_KEY_MAX_RATE_LIMIT` (10000), `WEBHOOK_MAX_ATTEMPTS` (6),
`WEBHOOK_TIMEOUT_SECONDS` (10), `WEBHOOK_DISABLE_AFTER_FAILURES` (20), `SUPPORT_CASES_PER_HOUR` (10).
`make prod-check` lists every required key still missing.

### On the server

1. `make prod-env`, fill in the keys, then `make prod-check` and `make compose-up-prod`.
2. Run `scripts/system_check.py`, `scripts/endpoint_sweep.py` and `scripts/security_probe.py` against the
   server's local ports.
3. Backups: confirm the nightly `make prod-backup` runs and an off-host copy exists; run
   `make prod-restore-check` once (restore drill).
4. Load test: `loadtests/` against the server; record the result in `docs/CAPACITY_PLAN.md`.
5. Stripe: register the webhook endpoint; send a test event. Prices are created by the deploy (P17-6).
6. Grant staff access (`is_staff`) to the people who answer support cases in the Django admin.

### Local machine notes

* Your Grafana login is `jt-code-admin`; on 2026-11-22 its password was reset to `GRAFANA_ADMIN_PASSWORD`
  from `.env.docker`.
* `.env.docker` sends Auth mail through real SMTP. The checks create confirmed users in the local database
  and send no mail. For the browser and integration tests GoTrue was pointed at Mailpit temporarily, and
  the API at the echo AI provider; both were restored after each run.
* Memory: on this 7 GB machine, parallel test runs plus the Docker stack made the out-of-memory guard
  (earlyoom) kill VS Code. Full test runs now use `-n 2`.
* Test logins created by the checks (`*@jt-code.local`) remain in the local Docker database.

## 6. Remaining to reach 95 %

1. The operator steps in section 5: keys, then the first run on the real server (TLS, backups with a restore
   drill, load test). This is the largest gap.
2. Decide on the connector framework (section 4).
3. Product decisions: MFA enrolment, a CAPTCHA provider.
