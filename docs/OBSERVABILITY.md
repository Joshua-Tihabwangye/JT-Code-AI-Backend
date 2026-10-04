# Observability and security hardening (Phase 15)

| Concern | Implementation | Configuration |
| --- | --- | --- |
| Errors | Sentry with PII scrubbing (`apps/core/sentry.py`) | `SENTRY_*` |
| Metrics | Prometheus (`apps/core/metrics.py`), `GET /metrics` | `METRICS_AUTH_TOKEN`, `CELERY_METRICS_PORT` |
| Dashboards and alerts | `infra/grafana`, `infra/prometheus/alerts.yml` | Grafana provisioning |
| Tracing | OpenTelemetry (`apps/core/tracing.py`), `infra/otel/collector.yaml` | `OTEL_*` |
| Audit | `apps/governance/audit.py`, append-only table, Kafka export | `AUDIT_ROUTE_RULES`, `AUDIT_EVENT_RETENTION_DAYS` |
| Edge and WAF | `infra/cloudflare`, `apps/core/edge.py` | `CLOUDFLARE_*`, `TRUSTED_PROXY_HOPS` |
| Webhook replay protection | `apps/core/signing.py`, `apps/core/webhooks.py` | `WEBHOOK_REPLAY_TOLERANCE_SECONDS` |
| Security gates | `manage.py security_gate`, CI (bandit, pip-audit, detect-secrets, OWASP ZAP) | `.zap/rules.tsv` |

## Sentry

`init_sentry` sets `send_default_pii=False`, never sends request bodies or
local variables, and installs `before_send` / `before_breadcrumb` hooks that:

* reduce request headers to an allowlist and drop cookies and query strings;
* reduce the user to its opaque id;
* replace values under sensitive keys (`password`, `token`, `secret`, `email`, ...);
* scrub e-mail addresses, bearer tokens, JWTs, API keys (`sk_`, `whsec_`,
  `sb_secret_`, `jtk_`, ...) and card numbers from every string.

Health checks and the metrics scrape are never traced.

## Metrics

`/metrics` needs `Authorization: Bearer <METRICS_AUTH_TOKEN>`. That token is
required in staging and production; without it, the endpoint exists only when
`DEBUG` is on. Cloudflare blocks `/metrics` at the edge, so scrape from the
private network.

| Metric | Labels |
| --- | --- |
| `jt_http_requests_total`, `jt_http_request_duration_seconds`, `jt_http_requests_in_progress` | method, route template, status |
| `jt_celery_tasks_total`, `jt_celery_task_duration_seconds` | task, state |
| `jt_ai_requests_total`, `jt_ai_tokens_total`, `jt_ai_provider_cost_usd_total`, `jt_ai_request_duration_seconds` | provider |
| `jt_usage_credits_charged_total` | feature |
| `jt_rate_limited_total` | dimension (ip/user/org), scope |
| `jt_webhooks_received_total`, `jt_security_events_total`, `jt_audit_events_total` | source/kind/category |
| `jt_outbox_published_total`, `jt_workflow_dispatches_total`, `jt_workflow_callbacks_total` | outcome |

The following gauges are read from PostgreSQL at scrape time and cached for
`METRICS_STATE_CACHE_SECONDS`:

* `jt_outbox_events`
* `jt_jobs_active`
* `jt_chat_requests_active`
* `jt_usage_open_reservations`
* `jt_stripe_events_unprocessed`
* `jt_dead_letter_events`
* `jt_workflow_runs_active`
* `jt_metrics_collector_up`

Routes are labelled by URL template, so raw IDs never become labels.

With several processes per container (gunicorn, Celery prefork), set
`PROMETHEUS_MULTIPROC_DIR`. Each Celery worker serves its registry on
`CELERY_METRICS_PORT`.

**Contract:** a test fails if a Grafana panel or alert rule queries a metric
that `apps/core/metrics.py` does not export.

## Dashboards and alerts

There are four provisioned dashboards (`infra/grafana/dashboards`):

* **API:** RED metrics and the slowest routes.
* **Workers, events and workflows:** Celery, outbox, DLQ and n8n.
* **AI usage and billing:** model runs, tokens, cost, credits and Stripe.
* **Security:** rejections, webhooks, audit and 401/403 responses.

`infra/prometheus/alerts.yml` defines page and ticket alerts for:

* error rate and latency;
* outbox backlog and dead letters;
* stuck jobs and workflows;
* Stripe failures;
* AI errors and spend spikes;
* signature-failure and origin-bypass bursts.

CI validates the rules with `promtool`.

## Tracing

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to the collector. Django, Celery, psycopg,
Redis and httpx are instrumented automatically. Trace context then crosses the
asynchronous boundaries:

* **Kafka:** the outbox stores `traceparent` with each event, and
  `run_kafka_consumer` continues the trace in a CONSUMER span.
* **n8n:** Django → n8n requests carry `traceparent`.

The request middleware also uses the OpenTelemetry trace id as `X-Trace-ID`, so
logs, Sentry events, outbox events and traces share one id. The collector
removes credentials and hashes client addresses before export.

## Audit pipeline

`record_audit_event` is the only writer. It does four things:

1. Stores an append-only row. A database trigger rejects UPDATE and DELETE,
   except for clearing a deleted actor and inside organization deletion or
   retention.
2. Redacts secrets from the metadata.
3. Publishes `governance.audit.recorded` through the transactional outbox for
   SIEM export.
4. Increments `jt_audit_events_total`.

Events are recorded from these sources:

* `AuditTrailMiddleware` records every mutating request to a sensitive route
  (`AUDIT_ROUTE_RULES`), including denied (401/403) attempts. Sensitive routes
  cover API keys, webhooks, integrations, tool policies, credentials, MCP,
  approvals, billing, organization and account settings, consents, retention,
  file deletion and n8n workflow administration.
* Django admin changes are mirrored from `LogEntry`.
* Forged, stale or replayed webhooks are recorded as platform-level security
  events.
* Tool gateway decisions are recorded.

`GET /api/v1/audit-events/` is limited to organization admins. It filters by
`category`, `severity`, `outcome`, `actor`, `start_date` and `end_date`.

Retention (`cleanup_old_audit_events`) applies tenant `audit_events` rules:
`hard_delete` or `anonymize`, with legal hold respected. Otherwise
`AUDIT_EVENT_RETENTION_DAYS` applies.

## Edge security

* **Security headers:**
  * API responses: `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`.
  * Admin and docs pages: a separate policy (`CSP_HTML_POLICY`).
  * Also set: `Permissions-Policy`, `Cross-Origin-Resource-Policy`, `nosniff`,
    `X-Frame-Options: DENY`, and `Cache-Control: no-store` on authenticated
    responses.
  * HSTS and TLS are set in `config/settings/secure.py` and at Cloudflare.
* **Client IP:**
  * `CF-Connecting-IP` is used only on requests that carry the Cloudflare
    origin secret.
  * Otherwise the `X-Forwarded-For` hop added by your own proxy is used
    (`TRUSTED_PROXY_HOPS`).
  * Otherwise `REMOTE_ADDR` is used.
  * Rate limits and audit rows use this resolved address.
* **Origin lock:** with `CLOUDFLARE_ENFORCE_ORIGIN=true`, requests without the
  `X-JT-Origin-Auth` header get 403. Health probes are exempt.
* **WAF:** `infra/cloudflare` provides:
  * the Managed and OWASP rulesets;
  * path, method and body-size rules;
  * an admin allowlist;
  * edge rate limits;
  * no caching of `/api/`.

## Webhook replay protection

| Webhook | Protection |
| --- | --- |
| Stripe | SDK signature check with `STRIPE_WEBHOOK_TOLERANCE_SECONDS`, plus `StripeEvent` idempotency |
| n8n callbacks, n8n error relay, custom Supabase senders | `X-JT-Code-Timestamp`, `X-JT-Code-Nonce` and `X-JT-Code-Signature: v1=HMAC-SHA256(secret, "ts.nonce." + body)` |
| Integration inbound webhooks | Timestamped HMAC and a replay cache |

For the n8n and Supabase scheme:

* The timestamp window is `WEBHOOK_REPLAY_TOLERANCE_SECONDS`.
* Nonces are consumed with `SET NX`, so a replay gets 409.
* Several secrets can be valid at once during rotation (`N8N_WEBHOOK_SECRET_PREVIOUS`).
* An unconfigured secret returns 503 (fail closed).

Supabase Database Webhooks (pg_net) cannot sign. They use a bearer secret, and
their events are idempotent state updates.

## Security gates

`python manage.py security_gate` runs:

* bandit (SAST);
* detect-secrets against `.secrets.baseline`;
* pip-audit (dependencies);
* evaluation of an OWASP ZAP report (`--zap-report`) when one is given.

CI runs the same scanners, then starts the API with gunicorn and runs
`zap-api-scan.py` against the OpenAPI schema with `.zap/rules.tsv`. FAIL rules
include XSS, SQL injection, command injection, error disclosure and missing
headers.

`tests/test_phase15_observability_security.py` also sends an anonymous request
to every operation in the schema. The test fails if any operation returns a
5xx, serves data, or omits the security headers.
