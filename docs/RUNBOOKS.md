# Runbooks

Each Prometheus alert (`infra/prometheus/alerts.yml`) links to its section here
through `runbook_url`; a test fails if an alert has no section. Every section
gives the impact, first checks, mitigation and when to escalate. The incident
process itself is in [INCIDENT_RESPONSE.md](INCIDENT_RESPONSE.md).

The examples below use these shell variables:

* `NS`: the namespace, `jt-code-production` or `jt-code-staging`.
* `k`: shorthand for `kubectl -n $NS`.

## sloerrorbudgetfastburn

**Impact:** at this rate the monthly 99.9% error budget (43 min) is gone in about
2 days. Treat as SEV-2: follow [apihigherrorrate](#apihigherrorrate), and roll
back first if a deploy is the cause.

## sloerrorbudgetslowburn

A sustained elevated error rate (budget gone in about 5 days). Find the failing
routes on the API dashboard and open a ticket; check `release_gate` before the
next release.

## apihigherrorrate

**Impact:** more than 2% of API requests fail with 5xx, so users see errors.

1. Grafana *JT-Code / API*: which routes? Sentry: which exception?
2. Look for a recent deploy: `k rollout history deploy/jt-code-api`. If the errors
   started with it, run `k rollout undo deploy/jt-code-api` (and the same for workers).
3. Check dependencies with `k exec deploy/jt-code-api -- curl -s localhost:8000/api/v1/health/ready/`:
   * **Database:** see [Supabase degraded](#supabase-degraded).
   * **Redis:** see [ratelimitstoreunavailable](#ratelimitstoreunavailable).
   * **Kafka:** see [outboxbacklog](#outboxbacklog).
4. If the errors are all on one AI route, see [aiprovidererrors](#aiprovidererrors).

**Escalate** to SEV-1 if it lasts more than 15 min or the error rate exceeds 10%.

## apihighlatency

**Impact:** p95 latency is above 2 s.

1. Look at the slowest routes panel and Sentry traces (slow spans: DB, Redis, HTTP).
2. Check database connections: `python manage.py capacity_audit --format=json`
   (`connections.inUse` against the pool). If saturated, scale the API down to
   relieve the pooler, or raise the Supavisor pool size.
3. Check CPU throttling (`kubectl top pods`). The HPA scales on CPU; raise
   `maxReplicas` if it is pinned at its ceiling.

## metricsstatecollectorfailing

Business-state gauges can't be read from PostgreSQL, usually a database outage
or a permission change. Check the API logs for `DatabaseStateCollector`. The
gauges fail independently, so alerts based on them go quiet; treat this as
reduced visibility.

## celerytaskfailures

1. Sentry: filter by the `celery.task` tag. Logs: `k logs deploy/jt-code-<worker> --since=30m`.
2. If one provider or integration is failing, see the matching runbook.
3. Poison input: the task retries and then fails. The job is marked failed and
   credits are released, so customers are not charged. Fix the cause, then
   re-run the job.

## outboxbacklog

**Impact:** domain events aren't reaching Kafka, so downstream consumers, n8n
notifications and SIEM export lag behind. Nothing is lost: events are durable in PostgreSQL.

1. Check the Kafka cluster health and credentials (SASL), then the
   `publish_outbox_batch` errors in Sentry.
2. Once Kafka recovers, the publisher drains the backlog on its own (watch the
   *Outbox backlog* panel).
3. If events are `failed` (attempts exhausted), see [outboxeventsfailed](#outboxeventsfailed).

## outboxeventsfailed

Events exhausted `EVENT_OUTBOX_MAX_ATTEMPTS`. Check `last_error` in the admin
(*Events → Outbox events*). After fixing the cause, reset them to `pending` in
the admin. They are idempotent by `event_id` downstream.

## deadlettergrowth

A consumer is rejecting events.

1. In the admin (*Events → Dead letter events*), read `error` and `event_type`.
2. Fix the handler or the data, deploy, then replay:
   `python manage.py replay_dead_letter <id>`. Each event can be replayed only
   once, and the consumer deduplicates.
3. The procedure is rehearsed by `python manage.py dlq_drill` (release-gated evidence).

## jobsstuckqueued

1. Check the queue depth panel and whether KEDA is scaling the worker
   (`k get scaledobject`).
2. If workers are crash-looping: `k describe pod`, `k logs --previous`.
3. Broker outage: jobs stay durable and `dispatch_queued_jobs` republishes them
   once Redis returns.

## workflowrunsbacklog

n8n workflow runs aren't completing.

1. Check n8n health (`n8n-main` and the workers), the queue depth in n8n's
   Redis, and the n8n error workflow alerts in Sentry.
2. Django retries silent executions after their `timeoutSeconds`
   (`sweep_workflows`); jobs fail cleanly after `maxAttempts`.

## workflowdispatchfailures

Django can't reach the n8n webhook processors, or they return 401 or 5xx.

1. A 401 means `N8N_DISPATCH_SECRET` doesn't match n8n's `JT_CODE_DISPATCH_SECRET`.
2. A 404 means the workflow is not active. Run `python manage.py n8n_workflows check`, then `push`.
3. Network or 5xx errors are retried with backoff automatically.

## stripeeventsfailing

**Impact:** payments, subscriptions or credits may not be applied.

1. In the admin (*Billing → Stripe events*), read `last_error`.
2. Fix the cause. `retry_failed_stripe_events` retries every 5 min, and the
   hourly `reconcile_stripe_billing` re-reads subscriptions and payments from
   Stripe, so the state converges.
3. Never edit credits by hand. Use reconciliation or a refund in Stripe.

## aiprovidererrors

1. Check the provider's status page and the gateway's circuit-breaker state
   (Sentry breadcrumbs).
2. The gateway falls back to the next model in the policy automatically. If every
   model of an alias is down, requests fail with `PROVIDER_UNAVAILABLE` and
   credits are released.
3. For a long outage, point the alias at the healthy provider in the admin
   (*AI gateway → Model aliases*).

## aispendspike

1. Grafana *AI usage and billing*: find the tenant or feature. The
   `CostAnomalyDetected` alert and `/internal/usage/anomalies/` identify the organization.
2. Abuse: suspend the organization or lower its plan limits. Bug (loop or
   runaway agent): disable the feature flag or alias.
3. Agent and RAG ceilings (`AGENT_MAX_COST_USD`, reservations) cap the cost of a single run.

## costanomalydetected

An organization's (or the platform's) hourly provider cost is far above its
14-day baseline. Investigate as in [aispendspike](#aispendspike). Confirm with
the customer before any suspension. The anomaly record shows the observed cost
against the expected cost.

## stalecreditreservations

Holds aren't being settled. `settle_finished_reservations` runs every minute;
check that the `jobs.default` worker and beat are running. Expired holds are
released automatically after `USAGE_RESERVATION_TTL_MINUTES`.

## webhooksignaturefailures

A burst of forged, stale or replayed webhook signatures.

1. Audit log (`category=security`, `action=webhook.rejected`): note the source
   IPs and the `reason`.
2. If the cause is a secret mismatch after a rotation, finish the rotation
   (`*_PREVIOUS` secrets). If it is an attack, block the source in the
   Cloudflare WAF.

## originbypassattempts

Requests are reaching the origin without Cloudflare's origin header. Check that
the origin firewall allows only Cloudflare's ranges. Rotate
`CLOUDFLARE_ORIGIN_SECRET` (Terraform `-replace` on the `random_password`) if
the secret may have leaked.

## ratelimitsurge

Sustained 429s. Check whether this is one client (Grafana *Security*, by
dimension) or a broad surge. For abuse, add a Cloudflare rule. For legitimate
growth, review plan `rate_multiplier`s.

## highseverityauditevents

An unusual volume of high-severity audit events (admin actions, credential or
tool-policy changes, denials). Review `/api/v1/audit-events/?severity=high`. A
compromised account is SEV-1: revoke the user's Supabase sessions and API keys.

## ratelimitstoreunavailable

**Impact:** Redis is unreachable. In-app rate limits fail **open**: requests
still succeed, while edge limits, quotas and credits still apply. Celery
dispatch and caching are degraded too.

1. Check the managed Redis status, credentials and TLS.
2. Jobs and chat requests are durable in PostgreSQL and are re-dispatched when
   Redis returns.
3. If Cloudflare shows abuse while this alert is firing, tighten the edge rate limits temporarily.

## supabase-degraded

Not an alert. Use this when `/health/ready` reports the database as failed.

1. Check the Supabase status page and your project's dashboard (connection
   count, CPU, disk).
2. If the pooler is saturated, reduce API and worker replicas, and confirm
   `DATABASE_CONN_MAX_AGE=0` with the transaction pooler.
3. For a planned failover or restore, set `READ_ONLY_MODE=true` (writes get 503
   with `Retry-After`, reads continue), then follow `BACKUP_RESTORE_RUNBOOK.md`.
