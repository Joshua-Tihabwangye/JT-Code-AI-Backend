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
  * a compose change also needs `python3 scripts/compose_config_check.py` (production, production + edge,
    local) and `tests/test_compose_stacks.py`.
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

## Phase 1: Identity profile integrity (F9, F14 + the reported bug) ☑

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

## Phase 2: API authentication hardening (F4, F5, F11, F12) ☑

| ID | Task | Files | Acceptance |
| --- | --- | --- | --- |
| P2-1 | **Session revocation.** With `SUPABASE_SESSION_CHECK=true`, the token's `session_id` must exist in `auth.sessions` for that user, and the user must not be banned or deleted. The result is cached in Redis for `SUPABASE_SESSION_CHECK_TTL_SECONDS` (default 30). Fail closed. On in both compose stacks; off in tests and native by default. | `apps/identity/sessions.py` (new), `authentication.py`, `base.py`, `compose.yaml` | Tests: a revoked session → 401; a banned user → 401; a cache hit makes no DB call; a token without `session_id` → 401 when the check is on |
| P2-2 | **Stricter JWT claims:** require `exp`, `iat`, `sub`, `role`; `aud` must equal `authenticated` in HS256 mode too; reject headers > 8 KiB; reject `alg` other than the configured mode | `authentication.py` | Tests for each rejection |
| P2-3 | **Failed-auth throttling.** More than `AUTH_FAILURE_LIMIT` (default 30) invalid tokens per IP per minute → 429 until the window ends | `authentication.py`, `apps/core/ratelimit.py` | Test |
| P2-4 | **Trusted proxies.** uvicorn `--forwarded-allow-ips` defaults to loopback + private ranges instead of `*`. Document `TRUSTED_PROXY_HOPS`. | `docker/entrypoint.sh`, docs | Test: the entrypoint has no `*` default |
| P2-5 | **Invite tokens hashed at rest** (SHA-256). Raw token returned once at creation; accept compares hashes. Migration hashes existing tokens. | `identity/models.py`, `views.py`, migration | Tests: accept works with the raw token; the DB stores only the digest |
| P2-6 | **Django admin:** `AUTH_PASSWORD_VALIDATORS` (length 12, common, numeric, similarity); `DJANGO_ADMIN_ENABLED` (default on, so the operator keeps DLQ access over an SSH tunnel); the public edge blocks `/admin` (P4-3) | `base.py`, `config/urls.py` | Test: admin URL absent when disabled |

Gate: BE-gate with `tests/test_identity_auth.py tests/test_supabase_identity.py tests/test_phase2_authz_tenancy.py tests/test_auth_hardening.py tests/test_compose_stacks.py`.

| P2-7 | (added during Phase 2, G1) `scripts/compose_config_check.py`: the documented `config -q` gate cannot run on the templates (empty `${VAR:?}` secrets, `env_file:` needs the real file names). The checker validates every stack from a temporary copy with dummy values. | new script | Prints `ok` for production, production+edge, local |

## Phase 3: Supabase Auth (GoTrue) and gateway hardening (F3, F6, F7, F10, F13) ☑

| ID | Task | Files | Acceptance |
| --- | --- | --- | --- |
| P3-1 | **Password policy in GoTrue**, matching the UI: `GOTRUE_PASSWORD_MIN_LENGTH=8`, required characters letters + digits; `GOTRUE_PASSWORD_HIBP_ENABLED` optional | `compose.yaml` | `docker compose config` shows them; ⚠ a live sign-up with `abcdefg` is rejected |
| P3-2 | **Session security:** refresh-token rotation + reuse interval 10 s; `GOTRUE_SESSIONS_INACTIVITY_TIMEOUT` (default 7 days) and `TIMEBOX` (30 days); password change requires reauthentication for sessions older than 24 h; secure e-mail change on; anonymous sign-ins off; manual identity linking off | `compose.yaml`, `.env*.example` | config check |
| P3-3 | **Per-IP rate limits.** The gateway resolves the real client IP (`set_real_ip_from` private ranges, `real_ip_header X-Forwarded-For`) and passes it as `X-JT-Client-IP` (always overwritten); GoTrue gets `GOTRUE_RATE_LIMIT_HEADER=X-JT-Client-IP` plus explicit limits (e-mail sent, verify, token refresh, OTP, anonymous) | `docker/supabase/nginx.conf`, `compose.yaml` | Test asserts the header is set and overwritten; ⚠ live: 2 IPs have separate budgets |
| P3-4 | **Gateway edge limits:** nginx `limit_req` on `/auth/v1/token`, `/signup`, `/recover`, `/otp`, `/verify`, `/resend` (per real IP, burst), returning 429 | `nginx.conf` | Test asserts the zones exist |
| P3-5 | **CORS allowlist.** Replace `*` with the origins in `SUPABASE_CORS_ORIGINS` (nginx template + `envsubst`); local default `http://localhost:5173`, production `FRONTEND_URL` | `docker/supabase/gateway/{default.conf.template,15-jt-cors-map.sh}`, `compose.yaml` | Tests; ⚠ live preflight from `:5173` passes and from `evil.example` gets no ACAO |
| P3-6 | **Close public paths:** a public listener (8000) denies `/rest/v1/` and `/auth/v1/admin/`; an internal listener (8001, not published) keeps them for Django (`SUPABASE_INTERNAL_URL=http://supabase-gateway:8001`) | `nginx.conf`, `compose.yaml` | Tests; ⚠ live: `curl :54321/rest/v1/` → 404, Django admin calls still work (`verify_supabase`) |
| P3-7 | Optional CAPTCHA (`GOTRUE_SECURITY_CAPTCHA_*` + FE widget), off until the operator picks Turnstile or hCaptcha | docs | Documented |

Gate: BE-gate with `tests/test_compose_stacks.py tests/test_gateway_hardening.py`; `compose_env.py check local` against `.env.docker`.

## Phase 4: Edge proxy, deployable frontend, API load balancing (F1, F8, F12) ☑

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

## Phase 5: Database pooling and least-privilege runtime role (F2, F15) ☑

| ID | Task | Files | Acceptance |
| --- | --- | --- | --- |
| P5-1 | **PgBouncer** (`edoburu/pgbouncer`, pinned), transaction mode, `max_prepared_statements` set. API, workers, beat and consumer use it (`DATABASE_POOLER_MODE=transaction`); `migrate` stays direct | `compose.yaml`, `docker/pgbouncer/` | config check; ⚠ live readiness ok |
| P5-2 | **Runtime role `jt_code_app`:** LOGIN, NOSUPERUSER, NOCREATEROLE, BYPASSRLS (Django tables have RLS without policies); DML on `public`; usage on sequences; `viz_data` CREATE + ownership of its tables; SELECT on only the `auth.sessions`/`auth.users` columns the session check needs; nothing else in `auth`/`storage`. Created and re-granted idempotently by `manage.py provision_app_role` in `migrate` when `APP_DB_PASSWORD` is set. Default privileges cover future migrations. | new command, `entrypoint.sh`, `compose.yaml`, `compose_env.py` | Test on the test DB (skipped without CREATEROLE); ⚠ live: app works and `select * from auth.users` as `jt_code_app` is denied |
| P5-3 | Validation: production refuses to run the app as `postgres` when `APP_DB_PASSWORD` is set but unused | `validation.py` | Test |

Gate: BE-gate with `tests/test_compose_stacks.py tests/test_db_roles.py`.

## Phase 6: Request path and caching (F15 follow-up) ☑ (pytest gate ⚠)

| ID | Task | Files | Acceptance |
| --- | --- | --- | --- |
| P6-1 | Memoize `organization_for_request`, membership and role checks on the request object (no cross-request cache, so no invalidation risk) | `apps/identity/authorization.py` | Test: query count drops for a typical endpoint |
| P6-2 | Revised during Phase 6 (G1): `/billing/plans/` and `/visualize/catalog/` require sign-in, so "public" caching does not apply. Both payloads are the same for every tenant, so they send `Cache-Control: private, max-age=300` (browser only, never a shared cache) instead of `no-store`. | billing/visualize views | Test |
| P6-3 | Document the cache inventory and TTLs in `docs/OBSERVABILITY.md` or a new `docs/CACHING.md` | docs | Doc review |

Gate: BE-gate with `tests/test_phase2_authz_tenancy.py tests/test_request_caching.py`.

## Phase 7: Frontend ↔ backend completion ☑ (pytest and npm gates ⚠)

| ID | Task | Files | Acceptance |
| --- | --- | --- | --- |
| P7-1 | **Team settings tab:** list members with their roles; invite by e-mail and role; change a role; remove a member. Uses the existing `/organizations/members/*` endpoints. Invite link is `/invite/:id?token=…`. | FE `features/settings/`, `repositories.ts`, `contracts.ts`, mock repo | FE-gate; contract regenerated; BE contract test green |
| P7-2 | **Accept-invite page** `/invite/:inviteId` (signs in first, then `POST …/accept/`) | FE route + page | FE test |
| P7-3 | Members payload includes name and `isOwner` | `apps/identity/views.py` | Test |
| P7-4 | Regenerate `tests/fixtures/frontend_api_contract.json` | BE | contract test green |
| P7-5 | (added during Phase 7, G1) **Workspace switcher.** The SPA never sent `X-Organization-ID`, so an invited user who accepted kept working in their own workspace and could never reach the team. `GET /organizations/` lists the user's workspaces with role, owner and default flags; the SPA remembers the choice (`src/lib/workspace.ts`), sends the header on every API call, forgets a workspace the user lost (403) and retries once; the Team tab switches and the accept page switches to the joined workspace. | `apps/identity/views.py`, `urls.py`; FE `src/lib/workspace.ts`, `http.ts`, data layer | Tests: list shows only own memberships with roles; FE header sent, stale workspace retried once |
| P7-6 | (added during Phase 7, G1) **Pending invites and revoke.** The FE repository (operator commit `6957dbe`) already called `DELETE /organizations/invites/{id}/` and read `invites` from the members list, which the backend did not serve. Members GET lists pending, unexpired invites (never the token); DELETE revokes (admins of that workspace only, status `revoked`); a second pending invite for the same e-mail is refused (409). | `apps/identity/views.py`, `models.py` (status value only, no migration) | Tests: revoked link → 404 on accept; non-admin 403; other workspace's admin 404 |

Gate: BE-gate (`tests/test_identity_auth.py tests/test_phase18_verification.py`), FE-gate + build.

## Phase 8: Backups and the deploy runbook (F16) ☑ (live checks ⚠)

| ID | Task | Files | Acceptance |
| --- | --- | --- | --- |
| P8-1 | `scripts/backup.sh`: `pg_dump -Fc` of the Supabase DB plus a tar of storage, rotated (`BACKUP_KEEP_DAYS`), optional off-host copy (`BACKUP_RSYNC_TARGET`) | new script | shellcheck clean |
| P8-2 | `make prod-backup` / `make prod-restore-check` (restore into a scratch DB and count rows) | Makefile | Dry run |
| P8-3 | `docs/SELF_HOSTED_DEPLOYMENT.md`: day-0 checklist (DNS, firewall, `.env.production` keys, GoTrue URLs, Stripe webhook URL, SMTP), day-2 (updates, scaling, rotation, restore drill) | docs | Doc review |

Gate: `bash -n` + shellcheck (when available) and BE-gate (`tests/test_compose_stacks.py`).

## Phase 9: Review close-out of Phases 1–8 (added 2026-11-18, G1)

Reason: a review of every completed phase against the audit. Phases 6–8 were committed from a workspace
without the project's Python or `node_modules`, so their gates never ran; running them found the FE
failures below. The review also found the defects in P9-1 and P9-2.

| ID | Task | Files | Acceptance |
| --- | --- | --- | --- |
| P9-1 | **Secret comparisons accept any header text.** `hmac/secrets.compare_digest` raises `TypeError` on non-ASCII `str`, so a crafted header turns the Cloudflare origin lock (every request), the metrics token, the Supabase webhook bearer, signed requests and inbound webhooks into a 500 instead of a rejection. One helper `apps.core.signing.constant_time_equals` compares UTF-8 bytes; every call site uses it. | `apps/core/signing.py`, `edge.py`, `metrics.py`, `identity/webhooks.py`, `integrations/views.py`, `assets/views.py`, `identity/views.py` | Tests: non-ASCII secrets → 401/403, never 500; the right secret still passes |
| P9-2 | **Invite accept and revoke lock the invite row** (`select_for_update` in one transaction), so an invite revoked or accepted concurrently cannot be accepted again | `apps/identity/views.py` | Tests: a revoked or accepted invite → 404 |
| P9-3 | `test_gateway_hardening` honours `JT_FRONTEND_DIR` like `test_edge_stack` (the frontend may be checked out under another name; the cross-check skipped silently) | test | Runs with `JT_FRONTEND_DIR` |
| P9-4 | FE gate fixes for Phase 7: type-only import in `workspaceHeader.test.ts` (lint error); `AcceptInvitePage.test.tsx` leaked a mock call between tests (fails in order) | FE tests | FE-gate green |
| P9-5 | ⚠ Run the database-backed pytest gates of Phases 6–8 (they need `TEST_DATABASE_URL`) | — | Operator, see the progress log |

Gate: BE-gate with `tests/test_core_secure_compare.py tests/test_team_management.py tests/test_gateway_hardening.py tests/test_edge_stack.py tests/test_compose_stacks.py tests/test_backup_script.py`; FE-gate + build.

## Phase 10: Responsive frontend: phones and tablets (added 2026-11-19, G1, FE only) ☑

Reason: the operator reports the UI works on large displays but not on tablets and phones. A browser audit
(every route at 360, 390, 768, 1024 and 1440 px, mock mode and against the local Docker backend with a real
dataset and dashboard) found the causes below. The audit becomes a repeatable test (P10-1).

| ID | Task | Files (FE) | Acceptance |
| --- | --- | --- | --- |
| P10-1 | **Responsive audit** `tests/e2e/responsive.e2e.mjs` (`npm run test:responsive`): every route at five widths; fails on content past the screen edge or a page that scrolls sideways; screenshots. Mock mode, or `RESPONSIVE_MODE=api` with an existing login (sign-up only with `RESPONSIVE_SIGNUP=1`, because local Auth may mail through real SMTP) | new script, `package.json` | Runs green in both modes |
| P10-2 | **Shell:** one 768 px breakpoint only. Tablets (769–1100 px) get the icon-only sidebar by default (the toggle still expands it). Overlays (shared `Modal`, visualize drawer and its backdrop) stack above the mobile top bar (z 70), which hid their headers | `AppShell.tsx`, `global.css`, `Modal.tsx`, `visualize.css` | Assistant header visible at 390 px; 1024 px shows the rail |
| P10-3 | **Visualize header:** the six sections wrap into a two-row pill on phones. ≤ 768 px: one row that scrolls sideways; Ask AI shrinks to the header row; the header stops being sticky (two rows would cover the page); the Python libraries strip is one scrolling line | `VisualizeLayout.tsx`, `visualize.css` | Header ≤ 2 rows at 360 px |
| P10-4 | **Ribbons (Data, Explore, …):** tabs scroll instead of wrapping; the pinned “Next” group covers most of a phone-width ribbon, so it scrolls with the rest ≤ 640 px | `visualize.css` | No control hidden behind the Next group at 390 px |
| P10-5 | **Dashboard viewer, share and embed:** below 900 px of canvas the authored 24-column grid is scaled down until text overlaps (slicers over KPIs, unreadable axes at 768/1024). New *flow* layout for 640–900 px: reading order (the page's phone order), 6-column CSS grid, KPIs/slicers three per row, charts two per row, wide visuals full width, natural pixel heights. Phones keep the stack, ≥ 900 px the authored grid | `DashboardPage.tsx`, `SharedDashboardPage.tsx`, `customize.ts`, `visualize.css` | Test for the flow spans; no overlap at 768/1024 |
| P10-6 | **Dashboard editor:** the 320 px pane leaves the canvas a sliver on tablets and phones. Below 1100 px the pane opens over the canvas and starts closed; the toolbar scrolls in one row on phones. Calculation rows showed empty boxes at every width (`.dash-fields button {width:100%}` also stretched the icon buttons) | `DashboardEditor.tsx`, `visualize.css` | Canvas uses the full width at 768; calc names visible |
| P10-7 | Remaining overflow found by the audit: gallery category chip at 1024, KPI “vs prev. month” at 768/1024; chat composer keeps room for the message on phones | `visualize.css`, `global.css` | Audit green |
| P10-8 | (added during Phase 10, G1) Grids that set columns only from a breakpoint up (`grid md:grid-cols-2`) let their single phone column grow to its widest content: Billing's invoice table made the page 612 px wide at 360. A base `grid-cols-1` (`minmax(0, 1fr)`) on all 25 such grids | 12 files | Audit green at 360 |
| P10-9 | (added during Phase 10, G1) Editor on narrow screens opens at 100 % zoom (Fit shrank the 1280 px page until chart text overlapped); its toolbar menus open as phone-width sheets (the scrolling toolbar would clip them; Insert is 430 px wide) | `DashboardEditor.tsx`, `visualize.css` | Readable canvas at 768 |

Gate: FE-gate + build; `npm run test:responsive` (mock) and `RESPONSIVE_MODE=api` against the Docker stack.

## Phase 11: Refreshing a page on the hosted site (Vercel) ☑ (added 2026-11-19, G1, FE only)

Reason: the operator reports that refreshing a page on the Vercel-hosted frontend crashes or answers 404.

| ID | Task | Files (FE) | Acceptance |
| --- | --- | --- | --- |
| P11-1 | **Deep links:** every app path is answered with `index.html` (rewrite, already present) and is proven by a test that serves the build with Vercel's rules (existing file first, then the rewrite, else 404) and reloads every route | `vercel.json`, `tests/e2e/refresh.e2e.mjs` | 21 routes: 200 and rendered after a reload |
| P11-2 | **Stale build after a deploy:** a tab (or a cached `index.html`) from the previous build asks for page files with old hashed names; the import fails and the crash screen appears. `vite:preloadError` now reloads once (at most once per 30 s, so a broken deploy shows the error instead of looping) | `src/lib/chunkReload.ts` (+ test), `main.tsx` | Test fails without the fix, passes with it |
| P11-3 | **Caching on Vercel:** `index.html` and every app path `no-cache` (always revalidated, so a refresh gets the current build); `/assets/*` immutable for a year. A missing `/assets` file stays a real 404 (never `index.html`, which the browser would try to run as JavaScript) | `vercel.json` | Checked by the test |

Gate: FE-gate + build; `npm run test:refresh`.


## Phase 12: Chat background fades behind a conversation ☑ (added 2026-11-20, G1, FE only)

Reason: the operator asks that the animated waves behind the chat fade once a question is asked, so the
question and the reply read clearly.

| ID | Task | Files (FE) | Acceptance |
| --- | --- | --- | --- |
| P12-1 | `ChatBackground dimmed`: with a conversation on screen (`hasThread`) the waves fade to 14 % opacity over 0.8 s, then the animation stops on that faint frame (no motion behind the text, no CPU); New chat brings them back. A theme change redraws the still frame in the new colours; reduced motion skips the transition | `ChatBackground.tsx` (+ test), `ChatPage.tsx`, `global.css` | Browser check: landing 1.0 → conversation 0.14 and paused → New chat 1.0 and animating |

Gate: FE-gate.

---

## Phase 13: Chat background: animated network wave ☑ (added 2026-11-20, G1, FE only)

Reason: the operator supplied a reference image (a glowing network of nodes and lines flowing diagonally,
lower left to upper right, with faint circuit traces) and asks for the chat background to be animated in
that style, with an organised motion. It is drawn on the canvas (not the picture itself): every node and
line can move, it stays sharp at any size, follows light and dark, and adds no download.

| ID | Task | Files (FE) | Acceptance |
| --- | --- | --- | --- |
| P13-1 | Geometry in a pure module: a seeded lattice of nodes (columns along the band, rows across it) around a spine that rises left → right; mesh edges (row neighbours and diagonals) and strands; per-node offsets fixed by a seed, motion only from one travelling swell, so the structure stays orderly | `src/features/chat/chatNetwork.ts` (+ test) | Deterministic; band rises; nodes inside the canvas band |
| P13-2 | Drawing: strands, mesh lines fading with length, glowing nodes (pre-rendered glow sprite), pulses travelling along the strands at even intervals, faint static circuit traces, top/bottom vignette; dark and light palettes | `ChatBackground.tsx` | Browser screenshots in both themes |
| P13-3 | Keep Phase 12 (fade and pause behind a conversation), reduced motion (one still frame), resize and theme changes | `ChatBackground.tsx` | Existing tests + browser check |

Gate: FE-gate + build.

---

## Phase 14: Phone drawer reaches its last item; sideways scrollers show they scroll ☑ (added 2026-11-20, G1, FE only)

Reason: the operator tested on an iPhone (Safari) and reports that the open navigation drawer shows only its
top items: Theme is cut off and the account row (sign in / create account) and the rest cannot be reached,
not even by scrolling. Cause: on phones the drawer is a full-height panel (`height: 100dvh`) whose footer is
pushed to the bottom (`margin-top: auto`). Safari's floating toolbar is drawn over the bottom of the
viewport, so the footer lies under it; the content still fits the panel, so there is nothing to scroll.
The operator also asks that the rows in Visualize that scroll sideways make that visible: on phones their
scrollbars are hidden (`scrollbar-width: none`) and nothing replaces them.

| ID | Task | Files (FE) | Acceptance |
| --- | --- | --- | --- |
| P14-1 | **Drawer (≤ 768 px):** the footer follows the navigation instead of being pinned to the bottom; bottom padding clears the safe area; the drawer scrolls on its own (`overscroll-behavior: contain`). Collapse is hidden in the drawer (the drawer always shows labels, so it did nothing) | `AppShell.tsx`, `global.css` | Audit: the drawer's last control ends ≥ 96 px above the bottom at 360/390/768 |
| P14-2 | **Scroll hints:** `useScrollHint` marks a sideways scroller with `data-more-left` / `data-more-right` while content is hidden on that side (scroll, resize, content changes; RTL aware); CSS fades that edge. The first time a row overflows in a visit it peeks (slides a little and back) unless reduced motion is set. Applied to the section nav, Python libraries, ribbon tabs and body, editor toolbar and page tabs. The editor toolbar drops the fade while a menu sheet is open (the mask would fade the sheet) | `src/lib/useScrollHint.ts` (+ test), `VisualizeLayout.tsx`, `PythonLibraries.tsx`, `Ribbon.tsx`, `DashboardEditor.tsx`, `visualize.css` | Unit test; audit: every overflowing scroller carries a hint |

Gate: FE-gate + build; `npm run test:responsive` (mock).

## Phase 15: Production-readiness verification with operator access ☑ (added 2026-11-22, G1, BE + FE; server steps ⚠)

Reason: the operator asks for proof that every frontend part reaches the right backend API, that every
endpoint works against the database, an end-to-end and security test pass, and a readiness verdict (their
bar: 95 %). Phases 1–14 left most live and database-backed checks ⚠ (no sudo, no test database). The
operator has now granted sudo on this machine, so those checks can run here. This phase verifies first and
fixes what the verification finds; new screens for API-only features are scoped in Phase 16 from P15-4's list.

| ID | Task | Acceptance |
| --- | --- | --- |
| P15-1 | **Test database and the full backend suite** (closes P9-5): a local-only superuser role `jt_code_tester` on the host PostgreSQL 18 (pgvector installed; `CREATE EXTENSION vector` needs a superuser), its URL kept outside the repo, `TEST_DATABASE_URL` passed per run | Full `pytest tests` result recorded; every failure fixed or reported |
| P15-2 | **System check without real mail:** the local `.env.docker` sends Auth mail through real SMTP, so the Mailpit confirmation step can never pass and each run mails a fake address. When the confirmation mail does not reach Mailpit, create the check user confirmed through the GoTrue Admin API (service key from `.env.docker`, read in-process, never printed), so no mail is sent | `scripts/system_check.py` all checks pass on the live stack |
| P15-3 | **Live endpoint sweep:** call every operation in the OpenAPI schema as a signed-in user on the Docker stack (safe methods with real ids; writes with minimal bodies); no 5xx | Script in `scripts/`, its report in the progress log; every 5xx fixed |
| P15-4 | **Two-way FE ↔ BE map:** every frontend call resolves to a backend operation (contract test), and every backend operation is listed as used by the frontend, or as server-to-server (webhooks, callbacks, health, metrics), or as API-only without a screen | List recorded; API-only features carried into Phase 16 |
| P15-5 | **Live security probes:** forged/expired/`alg: none` tokens, cross-tenant reads and writes on the main resources, CORS from a foreign origin, closed gateway paths, rate limits, security headers, upload type checks | Script, results in the progress log; every failure fixed with a test (G9) |
| P15-6 | **Dependency and static scans:** `pip-audit`, `npm audit --omit=dev`, `bandit` | High and critical issues fixed or justified |
| P15-7 | **Frontend end to end against the live stack:** `npm run test:integration` and `npm run test:e2e` in api mode | Results recorded |
| P15-8 | **Readiness report** `docs/PRODUCTION_READINESS_2026-11.md`: the score by area with its evidence, the keys the operator must add, the remaining ⚠ steps | File exists |

Gate: BE-gate (full suite) + FE-gate + build; `scripts/system_check.py` on the live stack.

## Phase 16: Screens for API-only features; dependency majors (added 2026-11-23, G1, BE + FE)

Reason: P15-4 found 268 of 432 operations without a frontend caller. About 80 are server-to-server or
operator-only by design. The others are user features with no screen, and the operator asks for every
backend feature to have its frontend part. P15-6 left three major upgrades. Order: what an enterprise
customer needs first.

| ID | Task | Acceptance |
| --- | --- | --- |
| P16-1 ☑ | **API keys, end to end.** Found 2026-11-23: keys were issued but **nothing accepted them** (`key_hash` was never read; the only authenticator was the JWT one). Also, a `PATCH` could set a revoked key back to `active`, and on `POST` the serializer returned the stored digest as `full_key`. Backend: `X-API-Key` authentication (active, unexpired, member, pinned to the key's workspace, `read`/`write` scopes, never key/account/member management, a per-key hourly limit, IP failure lockout); updates change the name only; scope, limit and expiry validation; members see their own keys, admins all. Frontend: **Settings → Developer: API keys** (create with scopes and expiry, shown once with copy, list, revoke) | BE tests for each rule (G9); repository + screen + unit tests; contract snapshot; live check |
| P16-2 ☑ | **Outbound webhooks, end to end.** Found 2026-11-23: endpoints could be registered but **nothing ever delivered to them** (the only `WebhookDelivery` writer was the inbound view), `test` was a stub, the signing secret was never shown, and URLs and headers were not validated. Backend: a published event catalogue whose organization is resolved from the event's entity (fail closed); deliveries created in the producer's transaction; signed (`X-JT-Signature`), SSRF-safe, no redirects, retried with backoff, endpoint disabled after repeated failures, beat sweep for due retries; a real test send; the secret shown once and rotatable. Frontend: **Settings → Developer: webhooks** (add, events, secret once, test, pause/resume, delete, recent deliveries) | BE tests for each rule (G9); repository + screen + unit tests; contract snapshot; live check |
| P16-3 ☑ | **Support cases, end to end.** Found 2026-11-24: customers could open cases but **nobody could answer** (no replies, no notification), and any editor could `assign` a case to **any user id in the system** or `PATCH` its status and assignee. Backend: a reply thread (`SupportCaseMessage`); members see their own cases, admins all of the workspace's; customers reply and close (no assign/resolve/edit); staff answer in the Django admin; mail to `SUPPORT_NOTIFICATION_EMAIL` and back to the customer; per-user hourly limit. Frontend: **Settings → Support** (open, list, thread, reply, close) | BE tests (G9); repository + screen + unit tests; contract snapshot; live check |
| P16-4 ☑ | **Settings → Audit log** (admins; filters; CSV export). Found 2026-11-24: a malformed `actor` filter or an impossible date answered 500; a date-only `end_date` dropped that day's own events (`parse_datetime` reads a bare date as midnight); the list never returned `outcome`. | BE tests (G9); repository + screen + unit tests; contract snapshot; live check |
| P16-5 ☑ | **Consents: personal and tamper-proof; "Your agreements" in Profile.** Found 2026-11-25: consent records are personal, but the API scoped them to the workspace, so any editor could list, change or **delete** other members' records, while viewers could not read their own. Records are legal evidence and could be deleted. Terms/privacy could be "withdrawn" through `PATCH` without deleting the account. The client supplied the IP and user agent. Nothing acts on the optional consents (marketing, analytics, AI training, third-party), so no switches are added for them (they would promise what the system does not do). Backend: own records only, any role; no `DELETE`/`PUT`; terms/privacy are recorded at sign-up and not withdrawn here (409: delete the account); the server records IP and user agent; withdrawal stamps `withdrawn_at`. Frontend: a read-only "Your agreements" section in Profile | BE tests (G9); repository + section + unit tests; contract snapshot; live check |
| P16-6 | **Automations, agents and agent runs** (list, create, run, history) | Same |
| P16-7 | **Tools and MCP** (servers, tool credentials, policies, approvals queue) and **prompts** | Same |
| P16-8 | **Dependency majors:** zod 4, vitest 5, puppeteer-core 25 | `npm audit` 0 high/critical; FE-gate, `test:e2e` |

Gate: FE-gate + build; BE-gate for any API change; `npm run test:integration` and `npm run test:e2e` on
the Docker stack.

---

## Progress log

### 2026-11-16: Phase 0 ☑
Audit, backlog and guardrails written. Contract snapshot regenerated: 157 calls, all resolve
(`pytest tests/test_phase18_verification.py -k contract`: 4 passed).

### 2026-11-16: Phase 1 ☑ (P1-8 ⚠)
- P1-1: `provision_on_authentication` fills empty profile fields from the token's `user_metadata` once per
  provisioning window (1 h), for existing rows too. Shared helpers `profile_values_from_metadata` /
  `fill_empty_profile` (also OAuth names and avatars). `dial_code` is capped at its column length (8;
  it was 10).
- P1-2: the webhook reads `raw_user_meta_data` or `user_metadata`. UPDATEs never blank or overwrite a
  field and never reactivate a suspended user.
- P1-3: Supabase-managed users get an unusable Django password (`!…`); migration
  `identity.0009_unusable_passwords` converts existing `''` rows.
- P1-4: `create_local_account` / `join_local_organization` take `--first-name/--last-name` and send them
  as Supabase `user_metadata`. `supabase_admin` gains `update_user_metadata` and `iter_auth_users`.
- P1-5: `PATCH /accounts/me/` writes `first_name`, `last_name` and `full_name` together.
- P1-6: `manage.py sync_supabase_profiles [--dry-run]`.
- P1-7: the sign-up page and the API repository send `terms_accepted` / `privacy_accepted` /
  `consent_version`. Provisioning records the two consents once; a withdrawal is never re-granted.
- P1-8 ⚠ operator: the native row for the operator's e-mail has no Supabase metadata to copy (it came from
  `create_local_account` without names). Fill it in Settings → Profile, or run
  `make native-up` and then `.venv/bin/python manage.py create_local_account <email> --first-name … --last-name …`
  (fills empty fields; the password is unchanged). For other old rows, run
  `manage.py sync_supabase_profiles --dry-run` first, then without `--dry-run`. The Docker stack applies
  migration 0009 on the next `make docker-up`.

Gate:
- ruff check + format: ok.
- `makemigrations --check`: no changes.
- pytest (`test_identity_auth`, `test_supabase_identity`, `test_identity_profile_sync` [12 new],
  `test_phase2_authz_tenancy`, `test_phase18_verification`): all green except
  `test_load_harness_runs_a_smoke_scenario_against_a_live_server`. That test failed once on latency
  (p95 7.4 s under `-n 6` with load average ~9). It passes alone, both on the baseline and with these
  changes, and it only hits endpoints this phase does not touch.
- FE: typecheck ok; lint 0 errors (the 62 warnings predate this phase; the changed files are clean);
  188/188 tests pass.

### 2026-11-16: Phase 2 ☑
- P2-1: `apps/identity/sessions.py`. With `SUPABASE_SESSION_CHECK=true` (set in `compose.yaml`, so
  both Docker stacks), a token's `session_id` must exist in `auth.sessions` for that user, unexpired,
  for a user who is not banned or deleted. Positive results are cached 30 s
  (`SUPABASE_SESSION_CHECK_TTL_SECONDS`). Missing `session_id` or a DB error → 401. The setting stays
  off by default (native, tests), per G8. Tested against a real minimal `auth` schema created in the test
  transaction.
- P2-2: `exp`, `iat`, `sub`, `aud`, `role` are required; the audience is always checked
  (`authenticated`); tokens over 8 KiB or non-ASCII → 401; other HS algorithms rejected.
- P2-3: more than `AUTH_FAILURE_LIMIT` (60) invalid tokens per IP per `AUTH_FAILURE_WINDOW_SECONDS` (60)
  locks the IP out for one window (429, metric `jt_security_events_total{kind="auth_failure_lockout"}`).
- P2-4: uvicorn trusts `X-Forwarded-For` only from loopback and private ranges (was `*`).
- P2-5: invite tokens are stored as SHA-256 digests (`hash_invite_token`); the raw token is returned once.
  Migration `identity.0010_hash_invite_tokens` hashes existing invites, so sent links keep working.
  The M2 test fixture in `test_security_hardening.py` now stores a digest; its assertions are unchanged.
- P2-6: `AUTH_PASSWORD_VALIDATORS` (12+ characters, common, numeric, similarity) for Django admin
  accounts; `DJANGO_ADMIN_ENABLED` (default on; the edge blocks `/admin` publicly in Phase 4).
- P2-7: `scripts/compose_config_check.py` (see the task).

Gate:
- ruff check + format: ok.
- `makemigrations --check`: no changes.
- `compose_config_check.py`: production, production+edge, local all ok.
- `pytest --create-db -n 4`: 122 passed across `test_identity_auth`, `test_supabase_identity`,
  `test_phase2_authz_tenancy`, `test_auth_hardening` (23 new), `test_compose_stacks`,
  `test_security_hardening`, `test_identity_profile_sync` and `test_conversions_research`.

⚠ Operator: `make docker-up` rebuilds the images (migrations 0009/0010, the new entrypoint, the session
check). Then sign out in the browser and confirm an old access token gets 401 within 30 s
(`curl -H "Authorization: Bearer <old token>" localhost:8000/api/v1/auth/ping/`).

### 2026-11-16: Phase 3 ☑ (live checks ⚠)
- P3-1: GoTrue password policy: `GOTRUE_PASSWORD_MIN_LENGTH` (default 8) and required characters letters
  + digits, matching the sign-up form. A test cross-checks the frontend schema.
- P3-2: refresh-token rotation (reuse interval 10 s); reauthentication for password changes on sessions
  older than 24 h; secure e-mail change; anonymous sign-ins and manual linking off; sessions idle limit
  168 h and lifetime 720 h (`SUPABASE_SESSIONS_*`).
- P3-3: `GOTRUE_RATE_LIMIT_HEADER=X-JT-Client-IP`. The gateway resolves the real client IP
  (`real_ip_header X-Forwarded-For` from loopback/private proxies only) and always overwrites the
  header. Explicit e-mail / verify / token-refresh / OTP / SSO limits (`SUPABASE_RATE_LIMIT_*`).
- P3-4: nginx `limit_req` (`SUPABASE_GATEWAY_AUTH_RATE`, default 30 r/m, burst 20) on token, signup,
  recover, otp, verify, resend, magiclink, reauthenticate and factors, per client IP; returns 429.
- P3-5: CORS allowlist from `SUPABASE_CORS_ORIGINS` (default `SUPABASE_SITE_URL`). It is generated
  at container start by `15-jt-cors-map.sh`, which accepts only exact `http(s)://` origins. The
  `Access-Control-Allow-Origin: *` is gone.
- P3-6: the public listener (8000, the only published port) returns 404 for `/rest/v1/` and
  `/auth/v1/admin*`. The internal listener (8001, unpublished) keeps them, and Django's
  `SUPABASE_INTERNAL_URL` now points at it. `docker/supabase/nginx.conf` was replaced by
  `docker/supabase/gateway/default.conf.template`.
- P3-7: CAPTCHA documented as an opt-in in both env templates (provider not chosen yet).
- Local stack (`compose.local.yaml`): same mechanisms with looser numbers, because every local request
  shares the Docker gateway's IP and the e2e suites sign up in bursts (gateway 600 r/m; e-mail and
  verify 1000/h; OTP 300/h).

Verification:
- The native GoTrue 2.196.0 binary (the image's version) loaded the hardened configuration and stopped
  only at the deliberately unreachable database. A bad duration is rejected, which shows the values are
  parsed.
- `compose_config_check.py`: production, production+edge and local all ok.
- `compose_env.py check local` against the real `.env.docker`: passes (107 values).

Gate:
- ruff check + format: ok.
- `makemigrations --check`: no changes.
- pytest: 74 passed (`test_gateway_hardening` [13 new], `test_compose_stacks`,
  `test_supabase_storage_client`, `test_supabase_identity`, `test_phase17_infrastructure`).
- Not verified here: nginx syntax. nginx is not installed and the Docker daemon needs sudo. The gateway's
  health check fails the stack if the config does not load.

⚠ Operator, after `make docker-up`:
1. `make docker-ps` shows `supabase-gateway` healthy.
2. `curl -si localhost:54321/rest/v1/ | head -1` → 404.
3. `curl -si -X OPTIONS -H 'Origin: https://evil.example' localhost:54321/auth/v1/token | grep -i access-control-allow-origin`
   prints nothing; the same request with `Origin: http://localhost:5173` prints it.
4. A sign-up with password `abcdefgh` (no digit) is rejected.
5. Sign-in, sign-up and reset still work in the browser.

### 2026-11-16: Phase 4 ☑ (live checks ⚠)
- P4-1 (FE `Dockerfile`):
  - API-mode build args replace the stale Clerk argument;
  - the build fails when `VITE_SUPABASE_URL`/`VITE_SUPABASE_ANON_KEY` are missing;
  - it serves from `nginx-unprivileged` on :8080;
  - `.dockerignore` excludes every `.env*`, because Vite would otherwise bake `.env.local` into the
    bundle.
- P4-2 (FE `nginx.conf`, now an nginx template, plus `nginx-security-headers.inc`):
  - CSP: scripts from self and Stripe.js only; no `unsafe-eval`; `object-src 'none'`; `connect-src`
    self + `JT_SUPABASE_ORIGIN` + Stripe + Sentry; fonts from Google Fonts;
  - `frame-ancestors 'none'` and `X-Frame-Options: DENY`, except `/embed/*` (embedding is a feature);
  - `/assets/*` immutable for a year; `index.html` `no-cache`; no API proxy.
- P4-3: `docker/edge/Caddyfile` with automatic HTTPS for `APP_DOMAIN` (SPA + `/api`) and
  `SUPABASE_DOMAIN`.
  - It closes `/admin`, `/metrics`, API docs/schema, `/rest/v1` and `/auth/v1/admin`.
  - It sends HSTS and flushes SSE immediately.
  - The `ACME_EMAIL` option was dropped: an empty value breaks parsing, and Let's Encrypt does not need it.
- P4-4: `web` service built from `FRONTEND_DIR` with the server's Supabase URL and anon key.
  `COMPOSE_PROFILES=edge` in `.env.production.example`, so `make compose-up-prod` starts `web` and
  `edge`.
- P4-5: `api` `deploy.replicas: ${API_REPLICAS:-1}` with loopback ports `API_HOST_PORTS`.
  - Caddy discovers the replicas (`dynamic a`, `least_conn`, passive health: 502–504 → out for 30 s,
    5 s retry).
  - Prometheus discovers the API and worker replicas by Docker DNS (`dns_sd_configs`).
- P4-6: `SELF_HOSTED_DEPLOYMENT.md` now covers the edge, DNS, firewall (including 443/udp), host-proxy
  alternative, the scaling table, auth hardening and the post-deploy checks.
- P4-7: FE `docs/ARCHITECTURE.md` rewritten (Supabase Storage, data modes, session check, image);
  README Docker section corrected.
- Test changes:
  - `test_messaging_production_readiness`: `_scrape_targets` also reads `dns_sd_configs`;
  - "exposes nothing publicly": the only allowed exception is `edge` on exactly 80/443/443-udp, and
    the new `test_only_the_edge_publishes_ports_beyond_loopback` pins that exact set.

Verification:
- Caddy 2.10.2 (`caddy validate`/`adapt`): valid with default and real domains; `caddy fmt` clean.
- `promtool check config`: ok (25 rules).
- `compose_config_check.py`: ok. `COMPOSE_PROFILES` from the env file activates `edge`/`web`.
- A clean copy filtered by the new `.dockerignore`, built with the image's environment: the bundle
  has `VITE_DATA_MODE:"api"` and the given Supabase URL, and no `localhost:54321`.

Gate:
- BE: ruff ok; no migration changes; 125 passed (`test_edge_stack` [10 new], `test_compose_stacks`,
  `test_messaging_production_readiness`, `test_gateway_hardening`, `test_phase17_infrastructure`,
  `test_phase15_observability_security`).
- FE: typecheck ok; lint 0 errors (62 pre-existing warnings); 188/188 tests pass.
- Not verified here: `docker build` and a running edge (they need the daemon, i.e. sudo).

⚠ Operator:
1. On an existing server with its own host proxy, keep `COMPOSE_PROFILES` empty: `compose_env.py init`
   appends new template keys, and the edge would collide on ports 80/443.
2. On the server: `make compose-up-prod`. Then check that `https://APP_DOMAIN` loads, that sign-in and
   chat streaming work, and that `/admin` is 404. Try `--scale api=3` with `API_HOST_PORTS=8000-8002`.

### 2026-11-17: Phase 5 ☑ (live checks ⚠)
- P5-1: `pgbouncer` service (`edoburu/pgbouncer:v1.25.2-p0`; tag and entrypoint variables checked
  against the published image):
  - transaction mode, SCRAM, `PGBOUNCER_POOL_SIZE` (default 40), `MAX_PREPARED_STATEMENTS=200`;
  - `pg_isready` health check, no published port;
  - API, workers, beat and consumer connect through it (`DATABASE_POOLER_MODE=transaction`,
    `CONN_MAX_AGE=0`) and wait for it to be healthy; `migrate` stays direct.
  - Transaction mode is safe because every session-level statement is transaction-scoped, Django
    binds parameters client-side, and the server time zone is UTC (Django issues no `SET TIME ZONE`).
- P5-2: runtime role `jt_code_app`, provisioned by `manage.py provision_app_role` in `migrate` (when
  `APP_DB_PASSWORD` is set) on every deploy.
  - Grants: DML on `public` plus default privileges; one permissive RLS policy per RLS table instead
    of BYPASSRLS; `viz_data` create/own plus `jt_viz_reader` membership; EXECUTE on the session
    function.
  - Denied: no DDL in `public`; no `auth` access; no superuser, CREATEROLE, CREATEDB, BYPASSRLS or
    replication (verified after provisioning).
  - Added during the phase (G1): migration `identity.0011_session_check_function`. `postgres` cannot
    grant `USAGE` on schema `auth`, so the session check now calls the `SECURITY DEFINER` function
    `public.jt_code_session_active(uuid, uuid)`, which answers yes/no for one session.
  - Bug found by the tests: a non-superuser owner (Supabase's `postgres` included) may not mention
    NOSUPERUSER/NOBYPASSRLS in `ALTER ROLE` on PostgreSQL 16+. The command now sets only LOGIN and the
    password and verifies the attributes instead.
  - `APP_DB_PASSWORD` is generated by `compose_env.py` (added to both templates and `GENERATED`).
- P5-3: validation refuses `postgres`/`supabase_admin` as the app user on a single host unless
  `DATABASE_ADMIN_CONNECTION=true` (set only on `migrate`).
- Also fixed: the five Phase 2 settings were missing from `.env.example`
  (`test_phase1_foundations` failed; it was not in the Phase 2 gate).
- Docs: `SELF_HOSTED_DEPLOYMENT.md` (PgBouncer, a roles table), `LOCAL_DOCKER.md`.

Verification:
- On the real local Supabase PostgreSQL 17, `provision()` ran in one transaction that was rolled back.
  It found 109 tables, created 109 policies and transferred 12 `viz_data` tables. The role saw 23
  users through RLS; it could create, grant and read a `viz_data` table as `jt_viz_reader`; the
  session function returned True for a live session and False for an unknown one; reading
  `auth.users` was denied. Nothing was left behind (0 roles, 0 functions, 0 policies).

Gate:
- ruff ok; no migration changes; `compose_config_check.py`: all ok.
- **Full suite** `pytest --create-db -n 6`: **968 passed**, 0 failed, 0 skipped.
- `compose_env.py check local` reports `APP_DB_PASSWORD` empty in the current `.env.docker`. That is
  expected: `make docker-up` runs `compose_env.py init` first, which appends and generates it.

⚠ Operator, after `make docker-up`:
1. `make docker-ps`: `pgbouncer` healthy, `migrate` exited 0 (its log shows `Runtime role jt_code_app: ...`).
2. The app works (sign-in, chat, a dashboard).
3. In pgAdmin as `postgres`: `select usename, count(*) from pg_stat_activity group by 1`. The app
   sessions are `jt_code_app`, and only a few server connections stay open.

### 2026-11-17: Phase 6 ☑ (pytest gate ⚠)
Implemented by the operator in `eef5640` ("Improved the backend"); reviewed and closed out here.
- P6-1: `apps/identity/authorization.py` memoizes the organization ids, the primary organization, the
  `X-Organization-ID` lookup and role checks in a dict that `SupabaseJWTAuthentication` attaches to the
  user object it builds for one request (`request_scoped_cache`). Users built any other way (Celery,
  tests, `force_authenticate`) are never memoized. A malformed or unknown organization id is still
  denied.
- P6-2 (revised, G1): `/billing/plans/` and `/visualize/catalog/` require sign-in, so they send
  `Cache-Control: private, max-age=300`. `apps/core/edge.py` uses `setdefault` for `no-store`, so the
  view's header is kept and every other authenticated response stays `no-store`.
- P6-3: `docs/CACHING.md` lists every cache layer, key, lifetime and why it is safe.

Review notes:
- No view changes memberships and then re-checks tenancy in the same request (members PATCH/DELETE and
  invite accept write after their last check), so the per-request memo cannot go stale.

Gate (run in a cloud workspace without the project's Python 3.14 environment or Django):
- `ruff check .`: all checks passed; `ruff format --check .`: 474 files already formatted.
- ⚠ Not run here: `makemigrations --check` and
  `pytest tests/test_phase2_authz_tenancy.py tests/test_request_caching.py -n 4` (Django and psycopg are
  not installed and package installs are blocked). Run them locally; Phase 6 adds no migrations.

### 2026-11-17: Phase 7 ☑ (pytest and npm gates ⚠)
- P7-3: `/organizations/members/` returns `name` (full name, else first + last, else display name) and
  `is_owner`; the owner is always reported as `admin` (matching `user_has_role`). Role PATCH and invite
  accept return the same payload; accept adds `organization: {id, name}`.
- P7-6: pending invites in the members payload, `DELETE /organizations/invites/{id}/` (status
  `revoked`), duplicate pending invite → 409.
- P7-5: `GET /organizations/` and the SPA workspace selection (header, stale-workspace recovery).
- P7-1 (FE): Settings has Profile and Team tabs (`?tab=team`). Team: workspace switcher; for admins,
  invite by e-mail and role (the link `/invite/:id?token=…` is shown once with a copy button), members
  with role select and remove (owner and yourself locked), pending invites with revoke. Non-admins see
  who to ask. Frontend-only mode keeps working (one local workspace; a note says invites need the API).
- P7-2 (FE): `/invite/:inviteId` behind `RequireAuth` (signed-out visitors sign in and return via
  `?next=`). Joining is an explicit click; on success the SPA switches to the joined workspace.
- i18n: new `team.json` area in all 20 languages (57 keys, same placeholders). The mock team
  repository's English `Error` strings (which the coverage test rejects) now use `i18n.t`.
- P7-4: contract snapshot regenerated: 164 calls (was 157; the 7 new ones are the team endpoints).

Verification (cloud workspace: no project Python 3.14 environment, no Django, no `node_modules`;
package installs are blocked there):
- BE: `ruff check .` and `ruff format --check .` clean. New `tests/test_team_management.py` (11 tests)
  written but **not run**. `models.py` only adds a `TextChoices` value to a field without `choices`, so
  no migration is expected.
- FE: every changed file parses (TypeScript `transpileModule`); the coverage test's hard-coded-text
  check (same code, run standalone) finds nothing; all 54 `team.*` keys used in code exist in English
  and every language has the same keys and placeholders. New tests: `src/lib/workspace.test.ts`,
  `src/lib/api/workspaceHeader.test.ts`, `src/lib/data/__tests__/team.test.ts`,
  `src/pages/__tests__/AcceptInvitePage.test.tsx` (**not run**).

⚠ Operator gate:
1. BE: `.venv/bin/python manage.py makemigrations --check --dry-run` and
   `.venv/bin/pytest tests/test_team_management.py tests/test_identity_auth.py tests/test_auth_hardening.py tests/test_security_hardening.py tests/test_request_caching.py tests/test_phase18_verification.py -n 4`.
2. FE: `npm run typecheck && npm run lint && npm run test && npm run build`.
3. Live: as the owner, Settings → Team → invite a second address as editor, copy the link, open it in
   a private window signed in as that address, join; the joiner lands in the team workspace and sees
   the owner's shared data; revoke a second invite and check its link answers "not found".
- Known limit: a new invitee who has no account signs up from the sign-in page, confirms the e-mail,
  and then opens the invite link again (sign-up does not carry `?next=`).

### 2026-11-17: Phase 8 ☑ (live checks ⚠)
- P8-1: `scripts/backup.sh`:
  - `pg_dump -Fc` of the whole database inside `supabase-db` as `supabase_admin` (falls back to
    `postgres`), verified with `pg_restore --list` before it is kept;
  - a tar of the Storage volume through a read-only `alpine:3.22` helper (`--volumes-from`), taken
    after the dump so the dump never references a missing file;
  - SHA-256 per file, directory 700 and files 600, rotation by `BACKUP_KEEP_DAYS` (only `jt-code-*`
    files), optional `rsync` to `BACKUP_RSYNC_TARGET`, `BACKUP_DRY_RUN=1`.
  - `BACKUP_DB_EXEC` / `BACKUP_STORAGE_DIR` run it without Docker (tests, other hosts).
- P8-2: `make prod-backup` and `make prod-restore-check`. The check verifies the checksum, restores
  the newest dump into `jt_restore_check_<UTC>` (dropped on exit, also on failure), and compares
  row counts with the live database: `django_migrations` must match, core identity tables must not
  be empty; other differences are listed as "changed since the backup".
- P8-3: `SELF_HOSTED_DEPLOYMENT.md`: day-0 checklist (DNS, firewall, domains, Auth URLs, SMTP,
  Gemini, Stripe webhook `https://APP_DOMAIN/api/v1/webhooks/stripe/`, Sentry, first backup and cron)
  and day-2 operations (updates with a pre-release backup, scaling, secret rotation per secret,
  backups and the restore drill, disaster restore). `BACKUP_RESTORE_RUNBOOK.md` points to it.
- `backups/` added to `.gitignore` and `.dockerignore` (dumps hold password hashes).

Verification:
- `bash -n` clean. shellcheck is not installed in this workspace (⚠ run `shellcheck -x scripts/backup.sh`).
- New `tests/test_backup_script.py` (8 tests + 1 shellcheck test that skips without shellcheck).
  pytest is not installed here, so the tests were executed through a minimal stand-in runner:
  7 passed as root; the end-to-end test (it skips as root because `initdb` refuses root) passed as
  the `postgres` user against a throwaway PostgreSQL 16 cluster: real dump, storage archive, rotation
  (a 30-day-old dump removed, an unrelated file kept), restore check passing, a migration mismatch
  failing, a damaged dump refused by its checksum, and no scratch database left behind.
- Two bugs found and fixed by that run: the cleanup trap referred to a function-local variable (the
  scratch database was not dropped), and a missing backup folder made the restore check exit
  silently under `pipefail`.
- `ruff check .` and `ruff format --check .` clean.

⚠ Operator, on the server:
1. `.venv/bin/pytest tests/test_backup_script.py tests/test_compose_stacks.py -n 4` (the end-to-end
   test runs where `pg_config --bindir` has `initdb`, as a non-root user).
2. `make prod-backup`, then `make prod-restore-check`; both must end without an error line. The first
   run pulls `alpine:3.22`.
3. Set `BACKUP_RSYNC_TARGET` and install the two cron lines from SELF_HOSTED_DEPLOYMENT.md; confirm the
   copy arrives off-host.
4. Once per quarter: the full disaster-restore drill on a spare server; record RTO/RPO.

### 2026-11-18: Phase 9 ☑ (database-backed pytest ⚠)
Review of Phases 0–8 against the audit, in a workspace with the project's `.venv` (Python 3.14.4) and
the frontend's `node_modules`. Local `Working_256` was fast-forwarded to the Phase 6–8 commits
(`df381d8`, `fcfc7bd`, `91dc131`; no history rewritten), then reviewed.

Review result, per phase:
- 1: provisioning fills empty fields only; the webhook reads both payload shapes and never blanks or
  reactivates; consents are recorded once. No change.
- 2: required claims and audience, 8 KiB cap, failure lockout, session check through the
  `SECURITY DEFINER` function (its default `anon`/`authenticated` grants are revoked by
  `governance.0011`), uvicorn trusts only private proxies, hashed invite tokens. Defect: P9-1.
- 3: the public gateway listener closes `/rest/v1` and `/auth/v1/admin`, always overwrites
  `X-JT-Client-IP`, and has the CORS allowlist and per-IP `limit_req`. No change.
- 4: Caddy closes `/admin`, `/metrics` and both docs/schema paths; the SPA image has a strict CSP and
  excludes `.env*`. Note: `FRONTEND_DIR` defaults to `../jt-code-frontend`; on this machine the
  checkout is `../JT-Code-AI-frontend`, so set `FRONTEND_DIR` in `.env.production`.
- 5: the runtime role has no DDL and no `auth` access and its attributes are verified; PgBouncer is in
  transaction mode and `migrate` connects directly. No change.
- 6: the memo lives on the per-request user only; `setdefault` keeps `private, max-age=300`. No change.
- 7: the team API requires an admin, protects the owner, accepts with the token digest plus the matching
  e-mail, and never returns the token again. Defect: P9-2 (accept/revoke race). The FE gate had never run:
  P9-4.
- 8: the backup script verifies the dump before it is kept, writes checksums, uses `umask 077`, has a dry
  run and drops the scratch database on exit. No change.

Changes:
- P9-1: `apps.core.signing.constant_time_equals` (UTF-8 bytes; empty or missing is unequal), used by the
  Cloudflare origin lock, the metrics token, the Supabase webhook bearer, signed requests, inbound
  webhooks, upload intents and invite accept. Run against the old code, the new tests fail with
  `TypeError` (HTTP 500) at `edge.py:48`, `metrics.py:297` and `webhooks.py:42`.
- P9-2: invite accept (`select_for_update(of=("self",))`) and revoke run in one transaction with the
  row locked.
- P9-3: `test_gateway_hardening` reads `JT_FRONTEND_DIR`.
- P9-4 (FE): `type AxiosHeaders` import; `AcceptInvitePage.test.tsx` clears the shared workspace mock
  before each test.

Gate:
- ruff check + format: ok (477 files). `makemigrations --check`: no changes.
- `compose_config_check.py`: production, production+edge, local ok. `bash -n scripts/backup.sh` ok.
- pytest without a database (`JT_FRONTEND_DIR=../JT-Code-AI-frontend`, `-n 4`): 58 passed, 2 skipped
  (no `caddy`, no `shellcheck` binary): `test_core_secure_compare` [12 new], `test_gateway_hardening`,
  `test_edge_stack`, `test_compose_stacks`, `test_backup_script` (its end-to-end run against a throwaway
  cluster passed).
- Contract snapshot regenerated from `../JT-Code-AI-frontend`: 164 calls, identical to the stored file.
- FE: typecheck ok; lint 0 errors (62 pre-existing warnings); 200/200 tests; `npm run build` ok.
- ⚠ Not run (P9-5): every database-backed test. No `TEST_DATABASE_URL` login is available in this
  workspace (no credentials for the host PostgreSQL, no `.env`/`.env.docker`, and pgvector is not
  installed on the host). That covers `test_request_caching`, `test_phase2_authz_tenancy`,
  `test_team_management` [2 new], `test_identity_auth`, `test_auth_hardening`,
  `test_security_hardening` and `test_phase18_verification`.

⚠ Operator:
1. Provide a test database: a PostgreSQL user with CREATEDB on a server that has pgvector, as
   `TEST_DATABASE_URL` in `.env`. Then run
   `.venv/bin/pytest --create-db -n 4 tests/test_request_caching.py tests/test_phase2_authz_tenancy.py tests/test_team_management.py tests/test_identity_auth.py tests/test_auth_hardening.py tests/test_security_hardening.py tests/test_phase18_verification.py tests/test_core_secure_compare.py`,
   and the full suite once.
2. The Docker stack running on this machine is a pre-Phase-2 image (uvicorn `--forwarded-allow-ips *`),
   and this checkout has no `.env.docker`. `make docker-up` creates one with new secrets; if the old
   database volume is kept, run `make docker-sync-db-passwords`. Then work through the live checks of
   Phases 2–8.

### 2026-11-19: Phase 10 ☑ (FE only)
How the problems were found: `tests/e2e/responsive.e2e.mjs` drives Chrome through every route at 360,
390, 768, 1024 and 1440 px, in mock mode and against the local Docker stack (signed in, the Retail sales
sample loaded, a dashboard built). It measures content past the screen edge and saves screenshots, which
were reviewed by eye as well: overlap and cramping do not show up as overflow.

Causes and fixes:
- Shell: one breakpoint at 768 px, so a 1024 px tablet kept the 260 px sidebar. Tablets (769–1100 px) now
  start with the icon rail (expandable for the visit; the saved desktop setting is untouched); the phone
  drawer always shows labels (`src/lib/useMediaQuery.ts`, `AppShell.tsx`).
- Overlays under the mobile top bar (z 70): the shared `Modal` (z 50) and the visualize drawer (z 60) hid
  their headers and close buttons on phones; now 85–90.
- Visualize header: the six sections wrapped into a two-row pill. ≤ 768 px: one sideways-scrolling row,
  Ask AI in the header row (icon only ≤ 420 px), the header no longer sticky, the Python libraries one
  scrolling line. Found on the way: the chips' `.sr-only` text (`position: absolute`) escaped the new
  scroll strips and widened the whole page to 507–1157 px; the strips are now `position: relative`. The
  audit now names positioned elements that escape a scroller.
- Ribbons: tabs scroll; ≤ 640 px the pinned “Next” group scrolls with the rest instead of covering it.
- Dashboards (viewer, share, embed): new tablet *flow* layout for canvases 640–900 px
  (`customize.ts` `dashboardLayoutMode`/`flowVisuals`, 2 new tests): the page's phone order on a 6-column
  grid, KPIs and slicers three per row, charts two per row, wide visuals full width, natural pixel heights.
  At 768 and 1024 px the slicers no longer sit on the KPIs and axis labels are readable. Phones keep the
  stack; ≥ 900 px the authored grid (1280 px laptops unchanged).
- Editor: below 1100 px the pane opens over the canvas and starts closed, the canvas opens at 100 %, the
  toolbar is one scrolling row on phones with its menus as sheets. Calculation rows showed empty boxes at
  every width (`.dash-fields button {width: 100%}` also stretched the icon buttons): fixed.
- Billing and 24 other grids (P10-8); chat composer: the model picker is an icon ≤ 420 px; the landing glow
  is capped at the container width; KPI comparison text ellipsizes.

Gate:
- FE: typecheck ok; lint 0 errors (62 warnings, unchanged; none added); 202/202 tests (2 new);
  `npm run build` ok.
- `npm run test:responsive` (mock): **98/98** after the fixes (before: billing at 360/390 and the chat glow
  failed); the 10 dashboard checks need the API and are listed as skipped.
- `RESPONSIVE_MODE=api npm run test:responsive` against the Docker stack: **108/108** route/viewport checks
  fit the screen; the editor re-run at 390/768 after the zoom change: 2/2.
- No API call changed, so the contract snapshot is unchanged.

Notes and ⚠ operator:
- The audit's api mode signs in with `RESPONSIVE_EMAIL`/`RESPONSIVE_PASSWORD`. Signing up
  (`RESPONSIVE_SIGNUP=1`) is only for stacks whose Auth mails through Mailpit: the local `.env.docker`
  sends Auth mail through real Gmail SMTP, and one sign-up during this phase produced a confirmation mail
  to a non-existent `@jt-code.local` address (it will bounce). That test login,
  `responsive-mwno1ava@jt-code.local`, was confirmed directly in `auth.users` and owns a few test
  dashboards in the local Docker database; delete it in Settings or keep it for the audit.
- Not covered by the audit: real devices (Safari on iOS, Android Chrome); touch gestures on the editor
  canvas. Try a dashboard and the editor on a real phone and tablet.

### 2026-11-19: Phase 11 ☑ (FE only; ⚠ deploy)
- P11-1: `vercel.json` already rewrote `/((?!api/|assets/).*)` to `/index.html` on every branch, including
  `origin/main`. `tests/e2e/refresh.e2e.mjs` (`npm run test:refresh`) serves a copy of `dist/` with exactly
  these rules and headers and opens, then reloads, 21 routes (public pages, every app page, visualize pages,
  a dashboard, share and invite links): all 200 and rendered, none on the crash screen.
- P11-2: the crash on refresh comes from stale page files after a deploy. With the fix removed, the test's
  stale-deploy step fails (no reload: the page crashes); with it, the app reloads exactly once, and a really
  missing file shows the error instead of reloading again.
- P11-3: `Cache-Control: no-cache` for the app shell, `immutable` for hashed assets.

Gate:
- FE: typecheck ok; lint 0 errors (62 warnings, unchanged); 204/204 tests (2 new); `npm run build` ok.
- `npm run test:refresh`: 24/24 (missing asset → 404, 21 route reloads, 2 stale-deploy checks).

⚠ Operator:
1. Vercel builds the branch it is connected to (normally `main`). Phases 10 and 11 are on the local
   `Working_256` only: push it and merge into `main` (a PR, as before) to deploy them.
2. In Vercel → Project → Settings → General, check that Root Directory is the repository root (where
   `vercel.json` is), Framework Preset is Vite and Output Directory is `dist`. If `vercel.json` is not read,
   no rewrite applies and every refresh outside `/` is a 404.
3. After the deploy, open `https://<your domain>/app/visualize/data`, refresh, and check the response in
   the browser's network tab: 200 for the page, `cache-control: no-cache` on it.
4. The frontend calls the API at `VITE_API_BASE_URL`. Vercel hosts no API, so on Vercel that variable must
   be the full backend URL (e.g. `https://api.example.com/api/v1`), not `/api/v1`. A relative `/api`
   path is answered by Vercel with its own 404, which looks like a broken page after a refresh.

### 2026-11-20: Phase 12 ☑ (FE only)
- P12-1: `ChatPage` passes `dimmed={hasThread}` to `ChatBackground`. The canvas fades to `opacity: .14`
  (`transition: opacity .8s`), and after the fade the draw loop stops; New chat (no thread) restarts it.
- Also verified on 2026-11-20 before this phase: Phase 11 (`6a967f7`) was committed after an interrupted
  session. Re-run here: `chunkReload` tests 2/2, build ok, `npm run test:refresh` all passed, and with the
  reload handler switched off the two stale-deploy checks fail (the test catches the crash).

Verification:
- Browser (mock mode, dark theme, guest chat as in the operator's screenshot): landing opacity 1 and not
  dimmed; after sending a message opacity 0.14, dimmed, and two canvas snapshots 1.5 s apart are identical
  (animation paused); after New chat opacity 1 and the canvas changes again (animating).
- FE: typecheck ok; lint 0 errors (62 warnings, unchanged); 206/206 tests (2 new).

### 2026-11-20: Phase 13 ☑ (FE only)
- P13-1: `src/features/chat/chatNetwork.ts`: a seeded lattice (11 row positions: 7 inside the band, 4 sparse
  outer rows; columns every 48–90 px), mesh edges, strands, 12 static circuit traces. The band's spine rises
  from 64 % of the height on the left to 34 % on the right and sways slowly; the only per-node motion is one
  swell travelling left → right through the columns. Tests (3): the same seed gives the same network; it
  covers the width and links only existing nodes; it rises left → right at several moments and no node
  moves more than a fifth of the spacing in half a second.
- P13-2: `ChatBackground.tsx` draws strands (each with two faint fibres), mesh lines batched in six
  brightness levels, the circuit traces, glowing nodes (one pre-rendered glow sprite; the shimmer travels
  along the band in sequence) and six pulses running left → right along the strands at even intervals.
  Time follows the real clock (same speed at 60 and 120 Hz; at most 50 ms per frame after a pause).
  Additive blending was tried for the dark theme and dropped: without a GPU it drew 21 frames per second
  against 45 with normal blending, for little visible difference.
- P13-3: Phase 12 unchanged (fade to 0.14 and pause behind a conversation, New chat resumes), reduced
  motion draws one still frame, resize rebuilds the network, a theme change redraws a paused frame.

Verification (Chrome, mock mode): dark and light at 1440×800 and a 390×844 phone. The network moves, with
no page errors. After a message: opacity 0.14, and two frames 1.2 s apart are identical (paused). After New
chat it animates again. Headless Chrome without a GPU ran at 45 (dark), 47 (light) and 60 (phone) frames
per second.

Gate:
- FE: typecheck ok; lint 0 errors (62 warnings, unchanged); 209/209 tests (3 new); `npm run build` ok.

### 2026-11-20: Phase 14 ☑ (FE only; ⚠ iPhone check)
- P14-1: on phones (≤ 768 px) the drawer's footer follows the navigation (`margin-top: 1rem` instead of
  `auto`), the drawer pads the bottom safe area and keeps its scrolling to itself. Collapse is hidden there
  (`.sidebar-collapse-toggle`); it changed nothing in a drawer that always shows labels. Tablet and desktop
  are unchanged.
- P14-2: `src/lib/useScrollHint.ts` sets `data-more-left` / `data-more-right` on a sideways scroller
  (scroll, resize, `ResizeObserver`, `MutationObserver`; right-to-left aware). `visualize.css` fades that
  edge over 2.75 rem with a mask. With a key (section nav, ribbon tabs, ribbon body, editor toolbar) the row
  slides 32 px and back once per visit the first time it overflows; reduced motion turns this off. Python
  libraries and the editor's page tabs get the fade only. The editor toolbar drops the mask while a menu
  sheet is open (`:has(.dash-menu)`), since the sheet is painted inside the toolbar.
- Audit (`tests/e2e/responsive.e2e.mjs`) gained two checks: an overflowing Visualize scroller without a hint
  fails; the open drawer fails if its last control ends within 96 px of the bottom and the drawer cannot
  scroll.

Verification (Chrome, mock mode): `npm run test:responsive` 98/98 (dashboard view and editor skipped in mock
mode, as before). The drawer check was run against the previous `global.css` and failed at 360, 390 and 768
px (last control at 724/828/1008 px, past the 96 px toolbar zone). With the fix it passes. Screenshots: the
drawer at 390 px ends with the account row right after Billing, and the Visualize section row fades out on
its right edge.

Not verified:
- ⚠ Real iPhone Safari (the operator's device). Chrome cannot draw Safari's floating toolbar. Check: open
  the drawer, see Theme and the account row under Billing.
- ⚠ Dashboard viewer and editor rows in api mode (`RESPONSIVE_MODE=api`, needs the Docker stack).

Gate:
- FE: typecheck ok; lint 0 errors (62 warnings, unchanged); 213/213 tests (4 new); `npm run build` ok.

### 2026-11-23: Phase 15 ☑ (server steps ⚠)
The operator granted sudo on this machine. Verification first; every defect it found was fixed with a test
that fails without the fix (G9). The full evidence and the score are in
[PRODUCTION_READINESS_2026-11.md](PRODUCTION_READINESS_2026-11.md).

Environment:
- Test database: role `jt_code_tester` (local-only superuser: `CREATE EXTENSION vector` needs one) on the
  host PostgreSQL 18. Its URL is in `~/.config/jt-code-tests/testdb.env` (mode 600, outside the repo).
  P9-5 closed.
- Live stack: the Docker stack, rebuilt from this branch twice (`docker compose … build api`, `up -d`;
  `make docker-up` was not used because it appends keys to `.env.docker`, G5). The machine was powered off
  three times during the phase; long runs were repeated after each boot.

Tasks:
- P15-1: full backend suite. The first run found 3 failures: two in team management (fixed below) and
  the DAST sweep's n8n callbacks. That test now configures the callback secrets, so it proves unsigned
  callbacks get 4xx. Unconfigured they fail closed with 503, so the result depended on the developer's
  `.env`.
- P15-2: `scripts/system_check.py` creates its user confirmed in the local `auth` schema when Auth mails
  through real SMTP (no mail to fake addresses). The chat check reports a failed request's code instead of
  timing out. The Grafana login is taken from `GRAFANA_ADMIN_USER`.
- P15-3: `scripts/endpoint_sweep.py` covers all 432 operations, about 990 calls (anonymous,
  reads with real ids, empty and schema-generated writes, cross-tenant probes, deletes), with 0 failures.
- P15-4: 164/164 frontend calls resolve. Of 268 backend operations without a caller, the user features
  go to Phase 16.
- P15-5: `scripts/security_probe.py`: 29/29.
- P15-6: pip-audit is clean after the upgrades (DRF 3.18.2: two unauthenticated DoS advisories; Pillow 13:
  42 advisories; pytest 9.0.3). `npm audit fix` covered axios, fast-uri and follow-redirects. zod 3 is
  justified, and vitest/puppeteer go to P16-8. Bandit: 1 medium, justified (the sandbox `exec`).
- P15-7: `npm run test:integration` 20/20 with echo AI providers (17/20 without an AI key: the AI
  features). `npm run test:e2e` 28/28 steps. For these runs only, GoTrue mailed to Mailpit and api/worker
  used echo providers (shell variables and an override file outside the repo); both were restored
  afterwards and checked.
- P15-8: readiness report: about 87 %, below the 95 % bar. What remains: Phase 16 and the operator steps.

Defects fixed:
- Team: an editor was listed as a viewer, because the members list and the workspace list took an
  arbitrary role row and a membership keeps its default viewer row. They now report the highest role.
- `POST /completion/`: every call answered 500 (credit hold `SELECT … FOR UPDATE` outside a transaction).
  Fixed with one transaction; autocommit tests.
- AI gateway: with no provider configured, the last fallback's error (the echo model) hid the primary's
  missing key. The chat now shows users a plain message; the cause stays in Sentry and the model run.
- `/embeddings/` 503 now carries `code: PROVIDER_NOT_CONFIGURED`.
- Prometheus: the API target was down since Phase 4 (replicas are scraped by IP, which Django refused as
  Host). The entrypoint exports `POD_IP`, which the settings allow, as Kubernetes already did.
- Security: a forged or unknown `X-Organization-ID` is refused on every endpoint (403). NUL in a path,
  query or JSON body is refused (400; it reached PostgreSQL: 500).
- Sign-up consent: parallel first requests raced (500 on first sign-in); the unique constraint now
  decides.
- `create_local_account` / `join_local_organization` failed on the Docker stack since Phase 3 (the Admin
  API is internal-only). In DEBUG they fall back to the stack's `auth` schema.
- FE: two loading messages nested a `<div>` (Spinner) in a `<p>` (a React DOM warning).
- FE e2e (stale since Phases 10 and 13 and the Nov 15 UI changes): sidebar clicks scoped to
  `nav.nav-links` (the phone top bar's hidden brand link matched first); Explore opens "All options"
  first. Two assertions now follow the current design instead of old exact values: the gallery categories
  wrap but stay visible, and conditional formatting repaints with theme colours. The forecast panel
  check follows its new title, the viewer step waits for the card value, and the colleague is created
  through the stack-aware command.

Gate:
- BE: ruff check + format ok (482 files). `makemigrations --check`: no changes.
  `compose_config_check.py`: production, production+edge, local ok. **Full suite: 1015 passed,
  2 skipped, 0 failed** (`pytest tests -n 3 --create-db`). Contract: 164 calls, `-k contract` green.
- FE: typecheck ok; lint 0 errors (62 warnings, unchanged); 213/213 tests; `npm run build` ok.
- Live: `system_check.py` 30/32 (2 need `GEMINI_API_KEY`); `endpoint_sweep.py` 0 failures;
  `security_probe.py` 29/29; `test:integration` 20/20; `test:e2e` 28/28.

⚠ Operator:
1. Add the keys in section 5 of the readiness report (Gemini, Stripe, SMTP, Sentry; n8n optional).
2. Grafana: your login `jt-code-admin` now has the `.env.docker` password (see the report).
3. On the server: `make compose-up-prod`, then the three live scripts, the backup restore drill and a
   load test.
4. Test logins created by the checks remain in the local Docker database (`*@jt-code.local`).

### 2026-11-23: Phase 16, P16-1 ☑ (API keys)
Found while scoping the screen: organization API keys were issued but nothing accepted them (`key_hash`
was written once and never read; the only authenticator was the JWT one). A key screen alone would have
handed customers keys that do not work. Two more defects in the same code:
- a `PATCH` could set a revoked key back to `active` (status was writable);
- on `POST` the serializer returned the stored digest as `full_key` (a placeholder).

Backend:
- `apps/integrations/authentication.py`: `X-API-Key` authentication, registered after the JWT one. It
  accepts active, unexpired keys of an active member, pinned to the key's workspace (another
  `X-Organization-ID` gets 403). `read`/`write` scopes (no `write`: safe methods only). It never serves
  `/api-keys/`, `/accounts/`, `/settings/`, `/organizations/` or `/tool-credentials/`. Each key has an
  hourly limit, capped by the new setting `API_KEY_MAX_RATE_LIMIT` (default 10000). Unknown keys count
  towards the per-IP failure lockout. `last_used_at` is written at most once a minute.
- `key_hash` is unique and indexed (migration `integrations.0003`).
- Serializers: responses never carry the key or its digest; the full key comes once, in the create
  response. Only `name` can change afterwards. Scopes must be `read`/`write` (omitted: `read`); the
  limit is capped; the expiry must be in the future.
- Viewset: members see and revoke their own keys, admins all; revoke is idempotent and returns the key.
- OpenAPI describes `X-API-Key` (`OrganizationAPIKey`); `docs/API_V1_CONTRACT.md` documents the rules.

Frontend:
- Settings → Developer (`?tab=developer`): create a key (name, expiry 30/90/365 days or never, an
  explicit "allow changes" box), shown once with copy and a `curl` example, the list (status, scopes,
  created, last used, expiry, owner), revoke with confirmation. `ApiKeyRepository` in API and mock modes.
  English texts plus 19 machine-filled languages; "Developer" corrected by hand in fr/es/zh (the
  machine chose the property and photo senses).

Tests (each rule fails without its code: without the authenticator, 8 of the API key tests fail):
- BE `tests/test_api_keys.py` (16): shown once, digest only; read key reads and cannot write; write key
  writes into its own workspace only; pinned workspace; no key/account/member management; revoked,
  expired, unknown and orphaned keys; no reactivation, only the name changes; invalid scopes, limit and
  expiry; default read scope; member versus admin visibility; per-key hourly limit.
- FE: `developer.test.ts` (2), `apiKeys.test.ts` (3: mock and API mapping), `DeveloperTab.test.tsx` (3).
- Live: `security_probe.py` has 8 API key probes; 37/37 on the rebuilt stack. Browser:
  `E2E_ONLY='API key' npm run test:e2e`: a key created in Settings reads the API from outside the browser,
  is refused for writes, and gets 401 right after revoke. GoTrue mailed to Mailpit for that run only and
  was restored.

Gate: BE full suite **1031 passed, 2 skipped, 0 failed**; ruff ok (484 files); `makemigrations --check` no
changes; contract 167 calls green. FE typecheck
ok, lint 0 errors (62 warnings, unchanged), 221/221 tests, build ok.

Note: `test_picture_of_a_table_is_read_by_ocr_into_rows_and_columns` failed once in a full run and passes
alone and in two other full runs: the OCR engine is memory-hungry next to 3 test workers on this 7 GB
machine.

### 2026-11-24: Phase 16, P16-2 ☑ (outbound webhooks)
Found: an organization could register webhook endpoints, but nothing ever delivered to them (the only
`WebhookDelivery` writer was the inbound view). `POST /webhooks/{id}/test/` was a stub that sent nothing,
the signing secret was never shown, and URLs and headers were not validated.

Backend (`apps/integrations/outbound.py`, `tasks.py`):
- Event catalogue (13 types: chat reply, job, document render, file conversion, knowledge indexing,
  images, agent run). Each type's organization is resolved from the entity behind the event key, never from
  the payload; with no resolvable organization nothing is sent (fail closed). Internal payload keys
  (`traceId`, `modelRunId`) are dropped.
- `add_outbox_event` fans out to subscribed active endpoints in the producer's transaction (as the n8n
  deliveries do). Delivery runs after commit on `jobs.default`; `sweep-webhook-deliveries` (beat, 60 s)
  re-dispatches due ones.
- Each delivery is signed (`X-JT-Signature: t=…,v1=HMAC-SHA256(secret, "t.body")`) and carries
  `X-JT-Event` and `X-JT-Delivery`. It goes out through `apps.tools.egress.safe_request`: HTTPS, public
  addresses only, pinned connection, no redirects, 64 KiB response cap, timeout. Backoff is 30 s × 4ⁿ up to
  6 attempts (`WEBHOOK_MAX_ATTEMPTS`). A lease on `next_retry_at` stops duplicate sends from a second task
  or the sweep; after `WEBHOOK_DISABLE_AFTER_FAILURES` (20) failed deliveries in a row the endpoint is
  disabled.
- API: admins only (an endpoint receives the whole workspace's events, so an editor's webhook would be an
  export channel). The `whsec_` secret is returned once at creation and by `rotate-secret`, never listed.
  Users may set only `active`/`paused` (resuming resets the failure count). URLs are checked with the
  egress rules when saved and again on each send; events must be in the catalogue; at most 10 custom
  headers, none of them reserved or `X-JT-*`, no CR/LF. `test` sends a real signed `webhook.test`.
- `docs/API_V1_CONTRACT.md`: how integrators verify signatures and how retries work.

Frontend: Settings → Developer → Webhooks (add with all or chosen events, secret once, send test,
deliveries with status, attempts and HTTP code (refreshed while open), pause/resume, new secret and
delete with confirmation, an admin-only note for other members). `WebhookRepository` in API and mock
modes; texts in 20 languages. The English "Rotate secret" became "New secret": "rotate" came back
literally ("spin the secret") in several languages, and Swahili's title as "web whips".

Tests:
- BE `tests/test_outbound_webhooks.py` (18). Without the outbox hook the 4 fan-out tests fail; the rest
  exercise code that did not exist. Related suites (billing, n8n, events, security): 94 passed.
- FE: repositories (3), section (4); 228/228 overall.
- Live: `security_probe.py` **44/44**. Unsafe URLs are refused (http, localhost, a private network
  address, port 8443). A test event to `https://example.com` (the IANA documentation domain) left through
  the worker and egress layer, and its answer (HTTP 405) was recorded. Another workspace does not see the
  endpoint.
- Browser (`E2E_ONLY='API key|webhook'`, GoTrue on Mailpit for that run only, restored): add a webhook,
  see the secret once, send a test, see "HTTP 405" in its deliveries, delete it.

Gate: BE full suite **1049 passed, 2 skipped, 0 failed**; ruff ok (487 files); `makemigrations --check` no
changes; contract 174 calls green. FE typecheck ok; lint 0 errors (62 warnings, unchanged); 228/228
tests. (The machine was powered off once during the gate; the suite was rerun.)

### 2026-11-24: Phase 16, P16-3 ☑ (support cases)
Found: customers could open support cases, but nobody could answer them (no replies, no notification).
Any editor could `POST /support-cases/{id}/assign/` with **any user id in the system** (no membership
check) and `PATCH` a case's status and assignee. No test or screen used the endpoints.

Backend:
- `governance.SupportCaseMessage` (migration `0013`; RLS through the existing `post_migrate` hook): the
  conversation, from the customer or from support.
- API: any member opens cases (validated subject, description, category, priority;
  `SUPPORT_CASES_PER_HOUR`, 429 after). Members see their own cases, admins all of the organization's;
  other workspaces get 404. `messages` (GET/POST; 409 once closed; a customer reply reopens a resolved
  case) and `close`. `assign`, `resolve`, `PATCH`, `PUT` and `DELETE` are gone for customers. Support
  staff appear as "JT-Code support", never by name.
- Staff answer in the Django admin: the case page shows the thread, and a reply typed in the empty row is
  stored as support, sets "waiting for user" and the first-response time.
- Mail (`apps/governance/support.py`, Celery on `jobs.default`, after commit, retried): new cases and
  customer replies go to `SUPPORT_NOTIFICATION_EMAIL` (empty: none); support replies go to the customer
  with a link to Settings → Support.
- The webhook views now use the existing `IsOrganizationAdmin` instead of a duplicate class added in
  P16-2 (G10).

Frontend: Settings → Support (`?tab=support`): open a case (topic, urgency, subject, description); the
list; a thread with support replies marked; reply; close with confirmation; a closed case takes no reply.
`SupportRepository` in API and mock modes. Texts in 20 languages; the brand is a `{{brand}}` placeholder
(machine translation turned "JT-Code" into "code JT"), and "Support" is set by hand in de/fr/es/pt/it/zh/ja
(the machine chose "helpfulness").

Tests:
- BE `tests/test_support_cases.py` (12): mail to the support inbox and none without it; who sees which
  cases; the conversation, a support reply mailed to the customer, a customer reply reopening the case;
  closed cases; no assign/resolve/edit; validation; rate limit; a reply typed in the Django admin is stored
  as support and mailed.
- FE: repositories (2), tab (3); 233/233 overall.
- Live: `security_probe.py` **50/50** (6 support probes). Browser: `E2E_ONLY='API key|webhook|support case'`
  4/4 steps (GoTrue on Mailpit for that run only, restored).

Gate: BE full suite **1061 passed, 2 skipped, 0 failed** (rerun after the machine restarted); ruff ok (489
files); `makemigrations --check` no changes; contract 179 calls green. FE typecheck ok; lint 0 errors (62
warnings, unchanged); 233/233 tests.

### 2026-11-24: Phase 16, P16-4 ☑ (audit log)
The audit API was real (admin-only, append-only, it records key, webhook, file and settings changes) but
had no screen, and its filters had defects:
- `actor=not-a-uuid` answered 500; so did an impossible date such as `end_date=2026-13-45`;
- `end_date=2026-11-20` excluded the 20th's own events (`parse_datetime` accepts a bare date as midnight,
  so the "date only" case was never reached); naive datetimes were compared with aware ones;
- the list never returned `outcome`.

Backend: the filters are validated (400) and an end date includes its whole day. New
`GET /audit-events/export/`: CSV of the filtered trail, newest first, streamed, at most 50 000 rows,
admin-only, workspace-scoped. Every cell starting with `=`, `+`, `-`, `@`, tab or CR is prefixed with `'`,
so an attacker-controlled description or user agent cannot run as a formula in Excel or Sheets.

Frontend: Settings → Audit log (`?tab=audit`): area and date filters (a reversed range is refused),
date-and-time, who, what (area, action, description), result, IP; "Show more" pages; Download CSV; an
admin-only note for other members. `AuditRepository` in API and mock modes (the mock records nothing);
texts in 20 languages.

Tests: BE `tests/test_audit_log_api.py` (10). The first four failed against the old code (500, missing
day, no export). The append-only trigger refuses `UPDATE`, so test events are dated when written. FE
repository (1) and tab (3); 237/237 overall. Live: `security_probe.py` **55/55** (5 audit probes). Browser:
`E2E_ONLY='API key|webhook|audit log|support case'` 5/5 steps; the screenshot showed an empty Result column
and date-only times, both fixed and checked again (GoTrue on Mailpit for those runs only, restored).

Gate: BE full suite **1071 passed, 2 skipped, 0 failed**; ruff ok (490 files); `makemigrations --check` no
changes; contract 181 calls green. FE typecheck ok; lint 0 errors (62 warnings, unchanged); 237/237 tests.

### 2026-11-25: Phase 16, P16-5 ☑ (consents)
Found: consent records are personal, but `/consents/` scoped them to the workspace and required write
access. An editor could list, `PATCH` or `DELETE` other members' records (legal evidence), and a viewer
could not read their own. Terms and privacy could be marked "withdrawn" without deleting the account, and
the client supplied the IP and user agent of a new record. Nothing in the code acts on the optional
consents (marketing, analytics, AI training, third-party), so no switches were added for them: they would
promise behaviour the system does not have.

Backend: own records only, in any role (others get 404); no `DELETE`/`PUT`. Creating is limited to optional
types with `granted`/`denied`; terms, privacy and the data processing agreement are recorded at sign-up
only. `PATCH` grants or withdraws an optional consent and stamps the time; on a required consent it answers
409 ("delete your account"). The server records IP (`client_ip`) and user agent.

Frontend: Settings → Profile → "Your agreements" (read-only): each consent, its version, when accepted or
withdrawn, and a note that terms and privacy end with the account. `ConsentRepository` in API and mock
modes; texts in 20 languages (zh "Accepted" corrected by hand).

Tests: BE `tests/test_consents_api.py` (5), all of which failed against the old code. FE: section (1) and
repository (1); 239/239 overall. Live: `security_probe.py` **59/59** (4 consent probes). Browser: after a
real sign-up the section lists the terms and privacy policy as accepted (`E2E_ONLY='agreements'`); the
other Settings steps still pass (GoTrue on Mailpit for those runs only, restored).

Gate: BE full suite **1076 passed, 2 skipped, 0 failed**; ruff ok (491 files); `makemigrations --check` no
changes; contract 182 calls green. FE typecheck ok; lint 0 errors (62 warnings, unchanged); 239/239 tests.
