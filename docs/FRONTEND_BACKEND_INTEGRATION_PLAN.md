# Frontend ↔ Backend Integration Plan

Connects `jt-code-frontend` (React 19 + Vite) to `jt-code-backend` (Django 5 + DRF, Celery, Kafka) running as the
local Docker stack (`make docker-up`: `compose.yaml` + `compose.local.yaml` + `.env.docker`), phase by phase,
starting with authentication. Each phase ends with a gate. The work items are in
[FRONTEND_BACKEND_INTEGRATION_BACKLOG.md](FRONTEND_BACKEND_INTEGRATION_BACKLOG.md).

## 1. What the scan found

### Frontend
- **Data mode switch:** `VITE_DATA_MODE` (`src/lib/config.ts`) chooses between `mock` (offline: browser-only auth and
  IndexedDB data, `src/lib/localSupabase.ts`, `src/lib/data/mock/`) and `api` (`src/lib/data/api/repositories.ts`).
  Everything above the `Repositories` interface (`src/lib/data/types.ts`) is the same in both modes. Connecting the app
  is a configuration change plus runtime verification, not a rewrite.
- **HTTP:** `src/lib/api/http.ts` is an axios instance on `VITE_API_BASE_URL` (`/api/v1`). Vite proxies `/api` to
  `http://localhost:8000`. A request interceptor adds `Authorization: Bearer <Supabase access token>`; supabase-js
  refreshes the token. SSE (chat streaming) uses `fetch` with the same token.
- **Auth:** `src/lib/supabase.tsx` uses supabase-js against `VITE_SUPABASE_URL` in API mode. It covers password
  sign-up, sign-in, OAuth, sign-out and the reset code flow. Offline mode shows a demo-account button that API mode
  hides.
- **Size of the API layer:** 151 HTTP calls (method + path), snapshotted by `scripts/contracts/extract_frontend_contract.py`
  into `tests/fixtures/frontend_api_contract.json`.

| Area | Calls |
| --- | --- |
| visualize | 60 |
| analysis | 11 |
| conversations | 11 |
| knowledge | 10 |
| documents | 9 |
| images | 9 |
| files | 8 |
| integrations | 6 |
| accounts | 5 |
| visualizations | 4 |
| chat | 3 |
| billing (plans, wallet, usage, invoices, subscriptions, payment methods) | 17 |
| settings | 1 |

- **Existing tests:**
  - unit tests: vitest, 183 tests;
  - `tests/integration/live-stack.test.ts`: the repository layer against a live stack, 22 cases across auth,
    account, chat, files, analytics, images, documents, knowledge, visualize, billing and isolation;
  - `tests/e2e/app.e2e.mjs`: puppeteer, 31 browser steps.

  Both live suites were written for the old native stack.

### Backend
- **API surface:** 293 OpenAPI paths under `/api/v1/`. **All 151 frontend calls exist with the right method**
  (checked against the live schema). Auth is delegated to Supabase GoTrue (ADR-001). Django verifies the Supabase JWT
  and provisions the user, organization, wallet and free plan on the first authenticated request.
- **Local Docker stack (running, all healthy):**

| Component | Containers / port |
| --- | --- |
| Supabase | db `:54322`, GoTrue/REST/Storage behind gateway `:54321`, Mailpit `:8025` |
| API | `:8000` |
| Celery | worker, documents worker, beat |
| Kafka | Redpanda, outbox consumer, Redpanda console `:8080` |
| Redis | cache and broker |
| Monitoring | Prometheus `:9090` (10 targets up), Grafana `:3000`, OTel collector, Tempo, Redis exporters |

  `/api/v1/health/ready/` reports database, redis, celery_broker and kafka as ok.
- **Local configuration:**
  - `AI_PROVIDER=echo` and `RAG_EMBEDDING_PROVIDER=echo`: chat and RAG answer without real keys.
  - Stripe uses a placeholder key, so checkout and portal return a clear configuration error.
  - n8n is external and not running.
  - OAuth providers are not configured.
  - `SUPABASE_MAILER_AUTOCONFIRM=false`: sign-up confirmation mail goes to Mailpit.
- **Backend-only surfaces with no frontend screen:**
  - agents and agent runs, automations, API keys, organization members and invites;
  - MCP servers, tools and approvals, prompts, policies;
  - support cases, webhooks, n8n callbacks, audit events, retention, internal usage.

  These are covered by the backend pytest suite. The system test probes them over HTTP where a user token is enough.

### Gaps to close
1. **`.env.local`:** it is in mock mode and has no anon key for the Docker Supabase. Use
   `python scripts/compose_env.py frontend local` to print the URL and anon key.
2. **Test credit grants:** the test helpers run the host's `.venv/bin/python manage.py grant_local_credits`, but the
   host `.env` points to the native Postgres (`:5432`), not the Docker database. The fix is
   `scripts/local_manage.py docker …`: it runs host `manage.py` against the Docker database on `127.0.0.1:54322`,
   reading `SUPABASE_DB_PASSWORD` from `.env.docker` in-process without printing it. No sudo and no container access
   are needed.
3. **Stale worker code:** the API container bind-mounts `apps/` and `config/`, but the workers, beat, consumer and
   migrate run the code baked into the image. Any backend fix needs `make docker-up` (rebuild, sudo) before the
   workers see it. The user runs it; Claude cannot sudo.
4. **Persistence checks:** nothing checks that a frontend action left rows in Postgres, events in Kafka, tasks in
   Celery and metrics in Prometheus. The new `scripts/system_check.py` does.

## 2. Approach
- One switch (`VITE_DATA_MODE=api`). No feature code forks. Mock mode stays available for design work.
- Each phase is verified at three levels:
  1. **API:** the integration cases for that area (`npm run test:integration -t "<area>"`) run the app's real repository
     layer against the live stack.
  2. **Browser:** the e2e steps for that area (`npm run test:e2e`) run against `npm run dev` in API mode.
  3. **Database:** `scripts/system_check.py` confirms the rows and events for the test user.
- When something fails, find the layer at fault (frontend mapping, backend view or serializer, worker, or
  configuration), fix it in the right repo and re-run that phase's gate. Backend fixes that touch worker code need a
  rebuild.
- Standing rules: commit only when asked (local branch `Working_256`), never push, no secrets in logs, and
  `.env*` changes are flagged for the user to reload.

## 3. Phases

| # | Phase | Scope | Gate |
| --- | --- | --- | --- |
| 0 | Baseline and tooling | Stack health, DB connectivity, `local_manage.py`, frontend env in API mode, test helpers pointed at the Docker DB | readiness ok; `manage.py check --database default` ok; unit suite green |
| 1 | **Auth** | Sign-up with Mailpit confirmation, sign-in, session to Django (JWT), workspace provisioning, sign-out, 401 without a session, forgot/reset code, OAuth error message, no demo button in API mode | integration "authentication" green; e2e sign-up / sign-out / sign-in green; `auth.users` + `identity` rows exist |
| 2 | Account and settings | Profile, avatar (Storage), change password, consents, export, delete account | integration "account settings" + e2e settings step |
| 3 | Chat and history | Conversations, SSE streaming through Celery, cancel, attachments, search, pin, archive, export, bulk delete | integration "chat and history" + e2e chat/history; Kafka chat topics advance |
| 4 | Files | Upload (Storage signature), rename, download, delete, restore | integration "files" |
| 5 | Documents | Create, edit, versions, duplicate, export, render | integration "documents" |
| 6 | Images | Generate, list, favourite, understand, edit, delete (stub or Gemini) | integration "images" |
| 7 | Knowledge (RAG) | Collections, sources, sync job, search, query | integration "knowledge" |
| 8 | Data analytics | Datasets, runs, visualizations, malformed-CSV error | integration "data analytics" |
| 9 | Data Visualization and dashboards | Import, profile, gallery, explore, predict, assistant, reports, dashboards (build, interact, customize, share, personal views, versions, export) | integration "data visualization" + e2e visualize/dashboard steps |
| 10 | Billing | Plans, wallet, usage, invoices, subscription, clear Stripe-not-configured errors | integration "billing" + e2e billing |
| 11 | Integrations | Connector list, connect / test / sync error paths | integration (added) |
| 12 | Isolation and security | Tenant isolation, CORS from `:5173`, unauthenticated 401s | integration "tenant isolation" |
| 13 | **Whole-system test** | Full integration and e2e suites; per-feature DB persistence; Redis (cache, broker), Celery (tasks succeed), Kafka (outbox published, topics advance, consumer up), Prometheus (targets up, app metrics move), Grafana (health, datasource, dashboards), OTel/Tempo, Mailpit, Storage | `system_check.py` all green; report written |

The order follows dependencies. Auth is first because every call needs a session. Account and chat come next
because they are the core loop. Files come before documents, images and knowledge, which store or attach files.
Visualization is the largest area. Billing reads the usage the earlier phases created.

## 4. Whole-system test (Phase 13) in detail
`scripts/system_check.py` (backend, stdlib + Django via `local_manage.py docker`) runs after the suites and prints a
pass/fail table:

| Service | Check |
| --- | --- |
| API | `/health/live`, `/health/ready` (db, redis, broker, kafka) |
| Postgres | Each feature wrote rows for the test users: users, orgs, wallets, conversations, messages, files, documents, images, knowledge, datasets, dashboards, usage records, credit ledger |
| Redis | `PING` on cache and broker; Celery queues drained |
| Celery | Recent `django_celery_results` / job rows succeeded; worker metrics present |
| Kafka | Outbox rows for the run are `published`; Redpanda console shows offsets advanced on `jt-code.local.*`; consumer target up |
| Prometheus | All targets `up`; `jt_code_http_requests_total`-style counters increased during the run |
| Grafana | `/api/health` ok; Prometheus datasource reachable; provisioned dashboards listed |
| Tracing | OTel collector and Tempo targets up |
| Mail / Storage | Mailpit received the confirmation and reset mails; Storage holds the uploaded objects |

## 5. Risks and decisions
- **Placeholder services locally:** echo AI, Stripe, OAuth and n8n. Their paths are verified for clear errors, not real
  results. Real keys belong in `.env.docker` when available.
- **Stale workers:** worker code is baked into the image. After backend fixes the user runs `make docker-up`.
- **E2E speed:** the e2e suite is long (31 steps). Phase gates run only that phase's steps where possible.
- **Mock mode is kept:** switching back is a one-line `.env.local` change.
