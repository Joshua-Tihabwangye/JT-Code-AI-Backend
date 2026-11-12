# Frontend ↔ Backend Integration Backlog

Executable work items for [FRONTEND_BACKEND_INTEGRATION_PLAN.md](FRONTEND_BACKEND_INTEGRATION_PLAN.md).

- **Paths:** `BE` is `jt-code-backend` and `FE` is `jt-code-frontend`.
- **Status values:** ☐ todo · ◐ in progress · ☑ done · ⚠ blocked (needs the user).
- **Prerequisite for every item:** the local Docker stack is up (`make docker-up` in BE).

## Phase 0: Baseline and tooling

| ID | Task | Command / change | Done when |
| --- | --- | --- | --- |
| P0-1 | Stack health | `curl localhost:8000/api/v1/health/ready/` | db, redis, celery_broker, kafka are `ok` |
| P0-2 | Host → Docker DB runner | BE `scripts/local_manage.py docker <manage args>`: host `manage.py` with `DATABASE_URL` to `127.0.0.1:54322`, password read from `.env.docker` and never printed | `scripts/local_manage.py docker check --database default` passes |
| P0-3 | DB connectivity test | `local_manage.py docker shell -c` with a SELECT, migrations applied (`showmigrations --plan` with no `[ ]`) | Connects; no unapplied migrations |
| P0-4 | Frontend env in API mode | FE `.env.local`: `VITE_DATA_MODE=api`, `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` from `python scripts/compose_env.py frontend local` | `npm run dev` boots with no config error |
| P0-5 | Test helpers use the Docker DB | FE integration and e2e `grantCredits` run `scripts/local_manage.py docker grant_local_credits` (override with `JT_CODE_MANAGE`) | Helper grants credits to a fresh user |
| P0-6 | Unit baseline | FE `npm run check` | Green |

## Phase 1: Auth

| ID | Task | Command / change | Done when |
| --- | --- | --- | --- |
| P1-1 | Sign-up + email confirm (Mailpit) + profile | `npm run test:integration -t "authentication"` | Sign-up, 401 and sign-out/in cases green |
| P1-2 | Workspace provisioning | `system_check` / shell: user, org, membership, wallet, free-plan credit ledger rows | Rows exist for the new user |
| P1-3 | Browser flow | e2e steps "sign up", "sign out protects the app", "sign in again" | Green with screenshots |
| P1-4 | API-mode UI | No demo button in API mode; a "failed to fetch" error shows the translated "service unreachable" message | Unit test + e2e screenshot |
| P1-5 | Forgot / reset code | Integration "resets a forgotten password with the emailed code" | Green |
| P1-6 | OAuth without providers | Clicking Google/GitHub shows a clear provider error, not a crash | Manual check / e2e |

## Phase 2: Account and settings
| ID | Task | Done when |
| --- | --- | --- |
| P2-1 | `-t "account settings"` (profile, avatar in Storage, password change, reset) | Green |
| P2-2 | e2e "settings: edit and save the profile" | Green; profile row updated |

## Phase 3: Chat and history
| ID | Task | Done when |
| --- | --- | --- |
| P3-1 | `-t "chat and history"` (SSE through Celery, attachment, list/search/pin/archive/export/delete) | Green |
| P3-2 | e2e chat + history steps | Green |
| P3-3 | Kafka: chat/conversation outbox events published | `system_check` Kafka section green |

## Phases 4–8: Files, documents, images, knowledge, analytics
| ID | Task | Done when |
| --- | --- | --- |
| P4-1 | `-t "files"` | Green; Storage object exists, then is deleted |
| P5-1 | `-t "documents"` | Green |
| P6-1 | `-t "images"` | Green (stub provider) |
| P7-1 | `-t "knowledge"` (Celery sync job, pgvector search) | Green |
| P8-1 | `-t "data analytics"` (isolated engine child, visualization) | Green |

## Phase 9: Data Visualization and dashboards
| ID | Task | Done when |
| --- | --- | --- |
| P9-1 | `-t "data visualization"` | Green |
| P9-2 | e2e visualize, gallery, explore, predict, assistant, reports and all dashboard steps | Green |

## Phase 10: Billing
| ID | Task | Done when |
| --- | --- | --- |
| P10-1 | `-t "billing"` (plans, wallet, usage, invoices, subscription; Stripe placeholder gives a clear message) | Green |
| P10-2 | e2e "billing shows the real plans and wallet" | Green |

## Phase 11: Integrations
| ID | Task | Done when |
| --- | --- | --- |
| P11-1 | Add an integration case: list integrations, connect/test error paths with no provider keys | Green |

## Phase 12: Isolation and security
| ID | Task | Done when |
| --- | --- | --- |
| P12-1 | `-t "tenant isolation"` | Green |
| P12-2 | CORS preflight from `http://localhost:5173` to `:8000`; 401 without a token | Checked in `system_check` |

## Phase 13: Whole-system test
| ID | Task | Done when |
| --- | --- | --- |
| P13-1 | Full `npm run test:integration` | All green |
| P13-2 | Full `npm run test:e2e` against `npm run dev` (API mode) | All 31 steps green |
| P13-3 | BE `scripts/system_check.py`: API, Postgres rows per feature, Redis, Celery, Kafka, Prometheus, Grafana, OTel/Tempo, Mailpit, Storage | All green; report printed |
| P13-4 | Backend-only surfaces probed over HTTP with a user token (agents, automations, api-keys, prompts, policies, MCP servers, tools, webhooks, support cases, audit events) | 2xx/403 as designed, no 5xx |
| P13-5 | Results written to the progress log below | Done |

## Progress log

### 2026-11-12: Phase 0 done; phases 1–12 green at the API level; browser blocked on two container restarts

**Phase 0 (baseline and tooling)**
- `scripts/local_manage.py docker …` connects to the Docker Postgres (17.6). No migrations are unapplied.
- FE `.env.local` is in API mode with the Docker anon key. The old key in the file was stale.
- The test helpers fund accounts in the Docker database. The `JT_CODE_STACK=native` variable switches them back to
  the native stack.

**Fixes found while connecting**
1. FE `src/i18n/config.ts` set `<html dir>` at import time, which crashed every Node consumer (the integration suite).
   It is now guarded.
2. The Docker stack requires email confirmation. The integration and e2e suites now confirm through the Mailpit
   link, as a user would. The app's own sign-up page already showed "check your email".
3. **Browser sign-in was broken ("failed to fetch"):** the nginx Supabase gateway had no CORS handling, and GoTrue
   rejects the `apikey` header that supabase-js sends. CORS is now answered at the gateway
   (`docker/supabase/nginx.conf`), as the native stack's Caddy did.
4. **The forgot-password code never arrived:** the Docker GoTrue used its default link-only recovery mail. It now
   uses the code template the native stack used (served by the gateway at `/templates/recovery.html`, wired with
   `GOTRUE_MAILER_TEMPLATES_RECOVERY`).
5. Fixes 3 and 4 take effect only after `scripts/compose.sh local up -d supabase-gateway supabase-auth`. ⚠ The user
   must run it (sudo).

**Integration suite against the Docker stack:** 19/20 green. That covers:
- auth;
- account (profile, avatar, password);
- chat over SSE through Celery, attachments and history;
- files in Storage, analytics, images, documents, knowledge;
- all of Data Visualization;
- billing, integrations (new case) and tenant isolation.

The forgot-password reset fails until fix 4 is live.

**`scripts/system_check.py`:** 29/33 on the first run.

| Area | Result |
| --- | --- |
| API | live and ready |
| Auth | sign-up, Mailpit confirmation, sign-in, JWT accepted, 401 without a session |
| Features | conversation, document, knowledge collection and dashboard created; the chat reply was written by a Celery worker |
| Celery | +34 tasks succeeded, 0 failed |
| Kafka | +4 events published, outbox empty, Redpanda offsets advanced, consumer polled 11 s ago, dead-letter queue empty, 59 topics |
| Redis | both instances up, +1,284 commands |
| Prometheus | 10/10 targets up, API metrics moved |
| Tracing | OTel collector and Tempo up |
| Storage | ok |
| Grafana | health ok |

Open items:
- The Supabase CORS check waits on fix 3.
- My Postgres query had wrong field names. Fixed; it needs a re-run.
- Grafana rejects the `.env.docker` admin password (it is applied only on the first volume init). ⚠ The user runs
  `make docker-grafana-reset`. The checker now makes a single login attempt per run, to avoid the 5-failure lockout.

**FE `npm run check`:** green (183 tests, build ok).
