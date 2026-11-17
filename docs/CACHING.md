# Caching

Every cache in the system, what it holds, how long, and why it is safe. Redis is never a source of
truth (ADR-005): losing a cache costs latency, never data.

## Browser and edge

| What | Where set | Lifetime | Notes |
| --- | --- | --- | --- |
| Hashed SPA assets `/assets/*` | FE `nginx.conf` | 1 year, `immutable` | File names change on every build |
| SPA shell (`index.html`, deep links) | FE `nginx.conf` | `no-cache` (revalidate) | A deploy is visible at once |
| World map GeoJSON `/api/v1/visualize/maps/world/` | `apps/visualize/views.py` | `public, max-age=86400` | Public data |
| Plan catalogue `/api/v1/plans/`, chart catalogue `/api/v1/visualize/catalog/` | billing / visualize views | `private, max-age=300` | Same for every tenant; browser only, never a shared cache |
| Every other authenticated response | `apps/core/edge.py` | `no-store` | Tenant data never lands in a disk or proxy cache |
| Edge (Caddy) | `docker/edge/Caddyfile` | none | Compression only (`zstd`, `gzip`); the API is never cached at the edge |

## Per request (process memory, one request)

| What | Where | Why it is safe |
| --- | --- | --- |
| Organization selection, membership ids, primary organization, role checks | `apps/identity/authorization.py` (`request_scoped_cache`) | Lives on the user object `SupabaseJWTAuthentication` builds for one request; cleared with it. Users built elsewhere (Celery tasks, tests) are never memoized |
| Resolved client IP | `apps/core/edge.py` | Per request |

## Redis cache (`REDIS_URL`, `allkeys-lru`, key prefix `jt-code:cache`)

| Key | Lifetime | Set by |
| --- | --- | --- |
| `identity:workspace-checked:<user>` (provisioning done) | 1 h | `apps/identity/provisioning.py` |
| `identity:session-ok:<session>:<user>` (live Supabase session) | `SUPABASE_SESSION_CHECK_TTL_SECONDS` (30 s) | `apps/identity/sessions.py`; positive results only, so a revocation applies within the TTL |
| Visualize / analytics engine results (op + request + data SHA) | per operation | `apps/visualize/services.py` (`_CACHEABLE_OPERATIONS`) |
| Dashboard query results (`visual_plan.query_key`) | per dashboard settings | `apps/visualize/dashboards.py` |
| AI circuit-breaker state | cooldown (`AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS`) | `apps/ai_gateway/resilience.py` |
| n8n workflow registry | short | `apps/orchestration/registry.py` |
| Webhook replay nonces | replay tolerance | `apps/core/signing.py` |

Rate-limit counters live in a separate alias (`rate_limits`, prefix `jt-code:rate-limit`): per IP,
user and tenant throttles (`apps/core/ratelimit.py`) and the invalid-token lockout
(`auth-fail:<ip>`, `auth-lock:<ip>`, `AUTH_FAILURE_*`). They fail open if Redis is unreachable, and
that is counted in `jt_security_events_total{kind="ratelimit_unavailable"}`.

## In process (per worker process)

| What | Lifetime |
| --- | --- |
| Supabase JWKS (when `SUPABASE_JWT_VERIFICATION=jwks`) | 1 h; refreshed at most once a minute for an unknown `kid` |
| Pre-loaded analytics engine (zygote) | process lifetime |

## Not cached, on purpose

* The API at the edge, and authenticated tenant data in browsers: `no-store`.
* Negative session checks: a revoked session is re-checked on every request.
* Database connections across requests: PgBouncer pools them (`docs/SELF_HOSTED_DEPLOYMENT.md`).
