# Execution backlog: profile integrity, auth hardening, single-host deployment

Source: [SYSTEM_AUDIT_2026-11.md](SYSTEM_AUDIT_2026-11.md) (finding ids `F1`–`F16`).
Rules for whoever executes it, human or agent: [../CLAUDE.md](../CLAUDE.md) (guardrails G1–G12).

* `BE` = `jt-code-backend`, `FE` = `jt-code-frontend`. Both work on the local branch `Working_256`.
  Commit per phase. **Never push.**
* Status: ☐ todo · ◐ in progress · ☑ done · ⚠ needs the operator (sudo, real server, real keys).
* A phase is done when every task is ☑ or ⚠ (with the operator step written down) and its **gate** passes.
  Record the gate output in the progress log at the end, then commit.

Standard gates (referenced below):

* **BE-gate:**
  * `.venv/bin/ruff check . && .venv/bin/ruff format --check .`
  * `.venv/bin/python manage.py makemigrations --check --dry-run`
  * the phase's targeted `pytest` files (`-n 4`);
  * a compose change also needs `docker compose -f compose.yaml --env-file .env.production.example config -q`
    and `tests/test_compose_stacks.py`.
* **FE-gate:** `npm run typecheck && npm run lint && npm run test`, plus `npm run build` when config or
  bundling changed.
* **Live check (⚠ operator):** `make docker-up` (rebuilds the images; needs sudo), then
  `python3 scripts/system_check.py` and the phase's manual check.

---

## Phase 0: Audit, backlog, guardrails ☑

| ID | Task | Done when |
| --- | --- | --- |
| P0-1 | Write `docs/SYSTEM_AUDIT_2026-11.md` (architecture, root cause, FE↔BE map, DB/LB/cache/events, findings, penetration surface, remaining work) | File exists, every finding has evidence |
| P0-2 | Write this backlog | Every finding maps to a task |
| P0-3 | `CLAUDE.md` guardrails in BE, and a pointer `CLAUDE.md` in FE | Files exist |
| P0-4 | Regenerate the frontend contract snapshot (157 calls) | `pytest tests/test_phase18_verification.py -k contract` green |

## Phase 1: Identity profile integrity (F9, F14 + the reported bug)

Goal: a user's Django profile always reflects their Supabase sign-up data, no path creates an
e-mail-only row, and existing rows can be repaired.

| ID | Task | Files | Acceptance |
| --- | --- | --- | --- |
| P1-1 | Apply sign-up metadata whenever provisioning runs (hourly window), not only on creation. Fill empty fields only; never overwrite profile edits. | `apps/identity/provisioning.py` | Test: an existing e-mail-only user whose token carries metadata is filled on the next request; an edited name is kept |
| P1-2 | Accept both payload shapes in the Supabase webhook: `raw_user_meta_data` (Database Webhook) and `user_metadata`. UPDATE never blanks a field; it updates the e-mail and fills empty profile fields through the same helper as P1-1. | `apps/identity/webhooks.py` | Tests: DB-webhook INSERT fills the name; an UPDATE with empty metadata keeps the name |
| P1-3 | Mark Django passwords as unusable (`!…`) for Supabase-managed users, on creation and through a data migration for existing `''` rows. Supabase holds the real bcrypt hash. | `authentication.py`, `webhooks.py`, management commands, `identity/migrations/00xx_unusable_passwords.py` | New users have `has_usable_password() == False`; the migration converts `''` rows |
| P1-4 | `create_local_account` / `join_local_organization` take `--first-name/--last-name` and send `user_metadata` to Supabase (`create_auth_user(..., metadata=...)`), and fill the Django profile | `supabase_admin.py`, both commands | Command test with a stubbed Admin API |
| P1-5 | `PATCH /accounts/me/` keeps `first_name`, `last_name` and `full_name` consistent; `serialize_account` prefers `first_name`/`last_name` | `apps/identity/account_views.py` | Test: a PATCH of `firstName` updates all three |
| P1-6 | `manage.py sync_supabase_profiles [--dry-run]`: page through the Supabase Admin API users and fill empty Django fields from `user_metadata` (repairs old rows, including ones created by the webhook) | new command | Test with a stubbed Admin API; dry run writes nothing |
| P1-7 | Record terms/privacy consent at first provisioning when the sign-up metadata says it was accepted. FE sends `terms_accepted: true`, `privacy_accepted: true` and `consent_version` in `options.data`. | `provisioning.py`, FE `signup.service.ts`, `repositories.ts` | Test: a new user with the flags gets two `ConsentRecord(granted)`; `/accounts/me/` shows `termsAccepted: true` |
| P1-8 | ⚠ Repair the operator's own native row: it has no Supabase metadata, so fill the profile in Settings, or run `create_local_account --first-name … --last-name …` again (the command now fills empty fields) | — | Operator action, documented |

Gate: BE-gate with `tests/test_identity_auth.py tests/test_supabase_identity.py tests/test_identity_profile_sync.py`; FE-gate.

## Phase 2: API authentication hardening (F4, F5, F11, F12)

| ID | Task | Files | Acceptance |
| --- | --- | --- | --- |
| P2-1 | **Session revocation.** With `SUPABASE_SESSION_CHECK=true`, the token's `session_id` must exist in `auth.sessions` for that user, and the user must not be banned or deleted. The result is cached in Redis for `SUPABASE_SESSION_CHECK_TTL_SECONDS` (default 30). Fail closed. On in both compose stacks; off in tests and native by default. | `apps/identity/sessions.py` (new), `authentication.py`, `base.py`, `compose.yaml` | Tests: a revoked session → 401; a banned user → 401; a cache hit makes no DB call; a token without `session_id` → 401 when the check is on |
| P2-2 | **Stricter JWT claims:** require `exp`, `iat`, `sub`, `role`; `aud` must equal `authenticated` in HS256 mode too; reject headers > 8 KiB; reject `alg` other than the configured mode | `authentication.py` | Tests for each rejection |
| P2-3 | **Failed-auth throttling.** More than `AUTH_FAILURE_LIMIT` (default 30) invalid tokens per IP per minute → 429 until the window ends | `authentication.py`, `apps/core/ratelimit.py` | Test |
| P2-4 | **Trusted proxies.** uvicorn `--forwarded-allow-ips` defaults to loopback + private ranges instead of `*`. Document `TRUSTED_PROXY_HOPS`. | `docker/entrypoint.sh`, docs | Test: the entrypoint has no `*` default |
| P2-5 | **Invite tokens hashed at rest** (SHA-256). Raw token returned once at creation; accept compares hashes. Migration hashes existing tokens. | `identity/models.py`, `views.py`, migration | Tests: accept works with the raw token; the DB stores only the digest |
| P2-6 | **Django admin:** `AUTH_PASSWORD_VALIDATORS` (length 12, common, numeric, similarity); `DJANGO_ADMIN_ENABLED` (default on, so the operator keeps DLQ access over an SSH tunnel); the public edge blocks `/admin` (P4-3) | `base.py`, `config/urls.py` | Test: admin URL absent when disabled |

Gate: BE-gate with `tests/test_identity_auth.py tests/test_supabase_identity.py tests/test_phase2_authz_tenancy.py tests/test_auth_hardening.py tests/test_compose_stacks.py`.

## Phase 3: Supabase Auth (GoTrue) and gateway hardening (F3, F6, F7, F10, F13)

| ID | Task | Files | Acceptance |
| --- | --- | --- | --- |
| P3-1 | **Password policy in GoTrue**, matching the UI: `GOTRUE_PASSWORD_MIN_LENGTH=8`, required characters letters + digits; `GOTRUE_PASSWORD_HIBP_ENABLED` optional | `compose.yaml` | `docker compose config` shows them; ⚠ a live sign-up with `abcdefg` is rejected |
| P3-2 | **Session security:** refresh-token rotation + reuse interval 10 s; `GOTRUE_SESSIONS_INACTIVITY_TIMEOUT` (default 7 days) and `TIMEBOX` (30 days); password change requires reauthentication for sessions older than 24 h; secure e-mail change on; anonymous sign-ins off; manual identity linking off | `compose.yaml`, `.env*.example` | config check |
| P3-3 | **Per-IP rate limits.** The gateway resolves the real client IP (`set_real_ip_from` private ranges, `real_ip_header X-Forwarded-For`) and passes it as `X-JT-Client-IP` (always overwritten); GoTrue gets `GOTRUE_RATE_LIMIT_HEADER=X-JT-Client-IP` plus explicit limits (e-mail sent, verify, token refresh, OTP, anonymous) | `docker/supabase/nginx.conf`, `compose.yaml` | Test asserts the header is set and overwritten; ⚠ live: 2 IPs have separate budgets |
| P3-4 | **Gateway edge limits:** nginx `limit_req` on `/auth/v1/token`, `/signup`, `/recover`, `/otp`, `/verify`, `/resend` (per real IP, burst), returning 429 | `nginx.conf` | Test asserts the zones exist |
| P3-5 | **CORS allowlist.** Replace `*` with the origins in `SUPABASE_CORS_ORIGINS` (nginx template + `envsubst`); local default `http://localhost:5173`, production `FRONTEND_URL` | `nginx.conf` → `docker/supabase/nginx.conf.template`, `compose.yaml` | Tests; ⚠ live preflight from `:5173` passes and from `evil.example` gets no ACAO |
| P3-6 | **Close public paths:** a public listener (8000) denies `/rest/v1/` and `/auth/v1/admin/`; an internal listener (8001, not published) keeps them for Django (`SUPABASE_INTERNAL_URL=http://supabase-gateway:8001`) | `nginx.conf`, `compose.yaml` | Tests; ⚠ live: `curl :54321/rest/v1/` → 404, Django admin calls still work (`verify_supabase`) |
| P3-7 | Optional CAPTCHA (`GOTRUE_SECURITY_CAPTCHA_*` + FE widget), off until the operator picks Turnstile or hCaptcha | docs | Documented |

Gate: BE-gate with `tests/test_compose_stacks.py tests/test_gateway_hardening.py`; `compose_env.py check local` against `.env.docker`.

## Phase 4: Edge proxy, deployable frontend, API load balancing (F1, F8, F12)

| ID | Task | Files | Acceptance |
| --- | --- | --- | --- |
| P4-1 | **FE image fix:** build args `VITE_DATA_MODE=api`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_API_BASE_URL`, Sentry; Clerk removed; the build fails fast when API mode lacks the Supabase args | FE `Dockerfile` | FE-gate + `npm run build` |
| P4-2 | **FE nginx:** strict CSP (self + Supabase origin for connect-src; `blob:`/`data:` where ECharts needs them), HSTS-ready security headers, immutable cache for `/assets/*`, `no-cache` for `index.html`, gzip, SPA fallback; no API proxy (the edge owns routing) | FE `nginx.conf` | Config test in BE (`tests/test_edge_stack.py`) |
| P4-3 | **`edge` Caddy service** (profile `edge`): automatic HTTPS for `APP_DOMAIN` and `SUPABASE_DOMAIN`. On `APP_DOMAIN`: `/api/*` → API upstreams, everything else → `web`. Blocks `/admin*`, `/metrics`, `/api/docs*`, `/api/schema*` publicly. On `SUPABASE_DOMAIN`: → gateway, minus `/rest/v1`, `/auth/v1/admin`. Sets `X-Forwarded-For` to the real client. | `docker/edge/Caddyfile`, `compose.yaml` | `docker compose --profile edge config -q`; Caddyfile test |
| P4-4 | **`web` service** builds `${FRONTEND_DIR:-../jt-code-frontend}` with the build args from `.env.production` | `compose.yaml`, `.env.production.example` | config check |
| P4-5 | **API horizontal scale:** `API_REPLICAS` (default 1) behind Caddy `dynamic a` upstreams with `lb_policy least_conn`, active health checks and retries; `WEB_CONCURRENCY` documented | `compose.yaml`, Caddyfile | config check; ⚠ live `--scale api=3` |
| P4-6 | Update `SELF_HOSTED_DEPLOYMENT.md`: edge profile, DNS, firewall, scaling commands, and how TLS, CORS and cookies follow from one app origin | docs | Doc review |
| P4-7 | Update the stale FE `docs/ARCHITECTURE.md` (Cloudinary → Supabase Storage; deployment) | FE docs | Doc review |

Gate: BE-gate with `tests/test_compose_stacks.py tests/test_edge_stack.py`; FE-gate + build.

## Phase 5: Database pooling and least-privilege runtime role (F2, F15)

| ID | Task | Files | Acceptance |
| --- | --- | --- | --- |
| P5-1 | **PgBouncer** (`edoburu/pgbouncer`, pinned), transaction mode, `max_prepared_statements` set. API, workers, beat and consumer use it (`DATABASE_POOLER_MODE=transaction`); `migrate` stays direct | `compose.yaml`, `docker/pgbouncer/` | config check; ⚠ live readiness ok |
| P5-2 | **Runtime role `jt_code_app`:** LOGIN, NOSUPERUSER, NOCREATEROLE, BYPASSRLS (Django tables have RLS without policies); DML on `public`; usage on sequences; `viz_data` CREATE + ownership of its tables; SELECT on only the `auth.sessions`/`auth.users` columns the session check needs; nothing else in `auth`/`storage`. Created and re-granted idempotently by `manage.py provision_app_role` in `migrate` when `APP_DB_PASSWORD` is set. Default privileges cover future migrations. | new command, `entrypoint.sh`, `compose.yaml`, `compose_env.py` | Test on the test DB (skipped without CREATEROLE); ⚠ live: app works and `select * from auth.users` as `jt_code_app` is denied |
| P5-3 | Validation: production refuses to run the app as `postgres` when `APP_DB_PASSWORD` is set but unused | `validation.py` | Test |

Gate: BE-gate with `tests/test_compose_stacks.py tests/test_db_roles.py`.

## Phase 6: Request path and caching (F15 follow-up)

| ID | Task | Files | Acceptance |
| --- | --- | --- | --- |
| P6-1 | Memoize `organization_for_request`, membership and role checks on the request object (no cross-request cache, so no invalidation risk) | `apps/identity/authorization.py` | Test: query count drops for a typical endpoint |
| P6-2 | Public catalog responses (`/billing/plans/`, visualize catalog) send `Cache-Control: public, max-age=300` when unauthenticated | billing/visualize views | Test |
| P6-3 | Document the cache inventory and TTLs in `docs/OBSERVABILITY.md` or a new `docs/CACHING.md` | docs | Doc review |

Gate: BE-gate with `tests/test_phase2_authz_tenancy.py tests/test_request_caching.py`.

## Phase 7: Frontend ↔ backend completion

| ID | Task | Files | Acceptance |
| --- | --- | --- | --- |
| P7-1 | **Team settings tab:** list members with their roles; invite by e-mail and role; change a role; remove a member. Uses the existing `/organizations/members/*` endpoints. Invite link is `/invite/:id?token=…`. | FE `features/settings/`, `repositories.ts`, `contracts.ts`, mock repo | FE-gate; contract regenerated; BE contract test green |
| P7-2 | **Accept-invite page** `/invite/:inviteId` (signs in first, then `POST …/accept/`) | FE route + page | FE test |
| P7-3 | Members payload includes name and `isOwner` | `apps/identity/views.py` | Test |
| P7-4 | Regenerate `tests/fixtures/frontend_api_contract.json` | BE | contract test green |

Gate: BE-gate (`tests/test_identity_auth.py tests/test_phase18_verification.py`), FE-gate + build.

## Phase 8: Backups and the deploy runbook (F16)

| ID | Task | Files | Acceptance |
| --- | --- | --- | --- |
| P8-1 | `scripts/backup.sh`: `pg_dump -Fc` of the Supabase DB plus a tar of storage, rotated (`BACKUP_KEEP_DAYS`), optional off-host copy (`BACKUP_RSYNC_TARGET`) | new script | shellcheck clean |
| P8-2 | `make prod-backup` / `make prod-restore-check` (restore into a scratch DB and count rows) | Makefile | Dry run |
| P8-3 | `docs/SELF_HOSTED_DEPLOYMENT.md`: day-0 checklist (DNS, firewall, `.env.production` keys, GoTrue URLs, Stripe webhook URL, SMTP), day-2 (updates, scaling, rotation, restore drill) | docs | Doc review |

Gate: `bash -n` + shellcheck (when available) and BE-gate (`tests/test_compose_stacks.py`).

---

## Progress log

### 2026-11-16: Phase 0 ☑
Audit, backlog and guardrails written. Contract snapshot regenerated: 157 calls, all resolve
(`pytest tests/test_phase18_verification.py -k contract`: 4 passed).
