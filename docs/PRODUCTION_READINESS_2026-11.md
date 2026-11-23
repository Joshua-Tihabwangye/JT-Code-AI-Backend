# Production readiness (2026-11-23, Phase 15)

The operator's bar for deploying is **95 %**. This report scores each area against evidence gathered on
this machine during Phase 15 (backlog: [EXECUTION_BACKLOG.md](EXECUTION_BACKLOG.md)). It says what was
verified, what was not, and what remains.

**Verdict: about 87 %. Not yet at the 95 % bar.** The code paths a user touches today work end to end
against the database and pass the security probes. The gap is in three places:

* operator keys and a real server, which only the operator can supply;
* screens for the backend features that have none (Phase 16);
* three development-tool upgrades.

## 1. Evidence

| Check | How | Result |
| --- | --- | --- |
| Backend unit and integration suite | `pytest tests` on PostgreSQL 18 + pgvector (`TEST_DATABASE_URL`, role `jt_code_tester`) | see the progress log (Phase 15) |
| Whole system, live | `scripts/system_check.py` on the Docker stack | 30/32. The 2 failures need `GEMINI_API_KEY` (chat reply, knowledge collection). |
| Every API operation, live | `scripts/endpoint_sweep.py`: 432 operations, about 990 calls (anonymous, reads with real ids, empty and schema-generated writes, cross-tenant, deletes) | 0 failures; no call slower than 3 s. The only 5xx are 5 operations that answer `503 *_not_configured` (Stripe ×3, Gemini ×2). |
| Attacks from outside, live | `scripts/security_probe.py` | 29/29 (section 3) |
| Frontend ↔ backend contract | `extract_frontend_contract.py`, `pytest -k contract` | 164 frontend calls, every one resolves to a backend operation |
| Frontend repositories against the live stack | `npm run test:integration` | 20/20 with the development echo AI providers; 17/20 without an AI key (the 3 AI features) |
| Real browser through the UI | `npm run test:e2e` (Chrome, 1366×900) | 28/28 steps: sign-up with e-mail confirmation, chat, history, data, gallery, Explore, Predict, every dashboard feature, public links, a viewer colleague, exports, settings, billing, sign-out, sign-in |
| Frontend unit tests, typecheck, lint | `npm run typecheck && npm run lint && npm run test` | see the progress log |
| Python dependencies | `pip-audit` | no known vulnerabilities (after the DRF 3.18.2 and Pillow 13 upgrades) |
| npm dependencies | `npm audit --omit=dev` | 1 high, `zod` 3 (justified, section 3). Development tools: section 5. |
| Static analysis | `bandit -r apps config` | 1 medium, justified: `exec` inside the isolated analytics engine child (the Python-visual sandbox) |
| Monitoring | Prometheus targets, Grafana | all 10 targets up; 5 dashboards provisioned; datasource works |

## 2. Score by area

| Area | Score | What holds it back |
| --- | --- | --- |
| Core user features (chat, history, images, visualize, dashboards, billing UI, settings, team) | 95 % | AI output needs `GEMINI_API_KEY`; payments need Stripe keys |
| Frontend ↔ backend integration | 92 % | Every screen reaches its API. 268 of 432 operations have no screen: about 80 are server-to-server or operator-only by design; the rest are features without a screen (section 4) |
| Security | 94 % | Section 3. Remaining: MFA enrolment UI, CAPTCHA provider (both product decisions), dependency majors |
| Data and events (PostgreSQL, pgvector, outbox, Kafka, Celery, Redis) | 93 % | Proven on this machine; no load test on the real server |
| Operations (deploy, TLS edge, backups, DR, monitoring) | 70 % | The production compose, Caddy edge and backup script are tested in config only. Never run on the real server: TLS, off-host backups, restore drill, load test |
| Overall (weighted towards what users touch) | **≈ 87 %** | |

## 3. Security

Found and fixed in Phase 15, each with a test that fails without the fix (G9):

| # | Finding | Fix |
| --- | --- | --- |
| S1 | `POST /completion/` held credits with `SELECT … FOR UPDATE` outside a transaction: **every** call answered 500 (hidden in tests by pytest's test transaction) | one transaction for job, credit hold, outbox and enqueue; autocommit tests |
| S2 | NUL characters in a query string reached PostgreSQL: 500 | `NulCharacterGuardMiddleware`: 400 for NUL in path, query or JSON body escapes |
| S3 | A forged `X-Organization-ID` was silently ignored by endpoints that never resolve a tenant (no data leaked: those scope by owner) | checked once per authenticated request: 403 for a workspace the caller is not in, with the messages the frontend uses to forget a stale workspace |
| S4 | Chat errors showed internal text to users (adapter names; with a missing key the key name; for unexpected errors the raw exception) | user-facing messages per code; the detail stays in Sentry and the model run |
| S5 | DRF 3.17: unauthenticated memory and CPU exhaustion (Accept header, charset codec); Pillow 12.3: 42 image-parsing advisories | DRF 3.18.2, Pillow 13.0 |
| S6 | Frontend: axios, fast-uri, follow-redirects advisories | `npm audit fix` (compatible versions) |
| S7 | A new user's parallel first requests raced on recording sign-up consent: 500 on first sign-in | the unique constraint decides; savepoint |

Verified in place (live probes): `alg: none`, wrong-key, expired, `service_role`/`anon`, wrong-audience,
session-less and tampered tokens all get 401. Mass assignment of `is_staff`/`is_superuser`/e-mail/plan is
ignored. Cross-tenant reads, patches and deletes on 49 real records are refused. CORS from a foreign origin
is refused on the API and on Supabase Auth. API responses carry CSP, `nosniff`, frame protection, a
referrer policy and `no-store`, with no server banner. The gateway closes `/rest/v1` and `/auth/v1/admin`.
GoTrue refuses weak passwords and rate-limits repeated wrong sign-ins (429 after 34).

Justified, not fixed:
* `zod` 3 (frontend, high): memory exhaustion when validating oversized constrained collections. Zod only
  validates the user's own form input in their own browser, so the user can only exhaust their own tab.
  The fix is zod 4, a breaking major; Phase 16 plans it.
* bandit B102 `exec` in `apps/visualize/viz_python.py`: the Python-visual sandbox. It runs only inside
  the isolated engine child (empty environment, rlimits, audit-hook guard), after AST validation.

## 4. Backend features without a screen

Server-to-server or operator-only by design (no screen needed): n8n callbacks and events, internal
endpoints, Kafka consumer administration, health, Stripe/Supabase/inbound webhooks, monitoring relays,
the model catalogue and providers, model policies and evaluations, safety events.

**User-facing features with an API but no screen (Phase 16):**
* API keys;
* outbound webhooks and their deliveries;
* support cases;
* the organization audit log;
* consents (privacy settings);
* agents and agent runs;
* automations;
* MCP servers, tools, tool credentials, policies and approvals;
* prompts;
* connectors.

## 5. What the operator must do

### Keys and settings (`.env.production` on the server; local tests use `.env.docker`)

| Purpose | Keys |
| --- | --- |
| AI (chat, images, embeddings for knowledge) | `GEMINI_API_KEY`; optional `GEMINI_DEFAULT_MODEL`, `GEMINI_EMBEDDING_MODEL`; optional fallbacks `OPENAI_API_KEY`, `LLAMA_API_BASE` + `LLAMA_API_KEY` |
| Payments | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PUBLISHABLE_KEY` (live keys in production) |
| E-mail (sign-up confirmation, password reset) | `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME` |
| Errors and tracing | `SENTRY_DSN`, `SENTRY_ENVIRONMENT`, `SENTRY_RELEASE`; frontend `VITE_SENTRY_DSN`, `VITE_SENTRY_ENVIRONMENT` |
| Workflows (optional) | `N8N_BASE_URL`, `N8N_WEBHOOK_BASE_URL`, `N8N_CALLBACK_BASE_URL`, `N8N_API_KEY`, `N8N_DISPATCH_SECRET`, `N8N_WEBHOOK_SECRET`, `N8N_SENTRY_RELAY_SECRET` |
| Domains | `APP_DOMAIN`, `FRONTEND_URL`, `SUPABASE_PUBLIC_URL`; frontend `VITE_API_BASE_URL` (full URL on Vercel), `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY` |

`make prod-check` lists every key that is still missing and refuses to start without it.

### On the server

1. `make prod-env`, fill in the keys, then `make prod-check` and `make compose-up-prod`.
2. Run `scripts/system_check.py`, `scripts/endpoint_sweep.py` and `scripts/security_probe.py` against the server's local ports.
3. Backups: confirm the nightly `make prod-backup` runs and an off-host copy exists. Then run `make prod-restore-check` once (restore drill).
4. Load test: `loadtests/` against the server; record the result in `docs/CAPACITY_PLAN.md`.
5. Stripe: register the webhook endpoint; send a test event.

### Local stack notes from this phase

* Your Grafana admin login is `jt-code-admin` (you renamed `admin`). On 2026-11-22 its password was
  reset to `GRAFANA_ADMIN_PASSWORD` from `.env.docker`. If you had set your own password in Grafana's UI,
  that one no longer works: sign in with the `.env.docker` value, then change it in Grafana. The system
  check takes the login from `GRAFANA_ADMIN_USER`.
* `.env.docker` sends Auth mail through real SMTP. The checks therefore create test users confirmed in the
  local database and send no mail. For the frontend's live tests, GoTrue was pointed at Mailpit
  temporarily, then restored.
* Test logins created by the checks (`system-*`, `sweep-*`, `probe-*`, `e2e-*`, `int-*` at `jt-code.local`)
  remain in the local Docker database.

## 6. Remaining engineering to reach 95 %

1. Phase 16: screens for the features in section 4, starting with API keys, webhooks, support cases and
   the audit log. Each needs a contract entry and tests.
2. Development tools: vitest 5 and puppeteer-core 25 (both majors; they clear 2 critical and 9 high
   advisories in tools that never ship). Also zod 4.
3. The operator steps above. Without a real-server run of TLS, backups and a load test, the operations
   score cannot pass 80 %.
