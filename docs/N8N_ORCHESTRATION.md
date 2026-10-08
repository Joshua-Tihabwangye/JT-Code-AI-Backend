# n8n orchestration (Phase 16)

n8n runs integration workflows. **Django/PostgreSQL stays the source of
truth** (ADR-005): n8n never touches the application database, and every state
change it reports is checked against Django state before it is applied.

Deployment, secrets and credentials: [infra/n8n/README.md](../infra/n8n/README.md).

## Where n8n is used

| Touchpoint | Trigger | Workflow (`n8n/workflows/`) | Reports back via |
| --- | --- | --- | --- |
| **Job workflows** | A job whose task type a workflow claims (`SCHEDULED_AUTOMATION`) | `scheduled-automation` | `/n8n/runs/{id}/status/`, `/n8n/runs/{id}/events/` |
| **Tenant automations** | `/automations/` on a cron schedule, or `POST /automations/{id}/run/` | `scheduled-automation` (one job per run) | as above |
| **Event notifications** | Outbox events (`billing.subscription.renewing_soon`, `governance.consent.expiring`, `jobs.job.failed`, `knowledge.document.index_failed`, `events.dead_lettered`, `safety.image_prompt_blocked`) | `event-notifications` | `/n8n/deliveries/{id}/status/` |
| **Knowledge integration sync** | Sync of a knowledge source of type `integration` (Drive, Notion, GitHub, Slack) | `knowledge-integration-sync` | `/n8n/knowledge/sources/{id}/documents/` and delivery status |
| **Integration checks** | `/integrations/` connect, test or reconnect | `integration-test` (synchronous) | HTTP response |
| **Error workflow** | Any JT-Code workflow fails | `error-handler` | `/n8n/errors/` → Sentry, audit, Kafka and a retry |
| **Workflow events** | A workflow publishes a business event | any | `/n8n/events/` → Kafka `orchestration.n8n.<name>` |

## Security

**Django → n8n.** Every dispatch is a `POST {N8N_WEBHOOK_BASE_URL}/webhook/<path>`.

* It is signed with `N8N_DISPATCH_SECRET`:
  `X-JT-Code-Timestamp`, `X-JT-Code-Nonce`, and
  `X-JT-Code-Signature: v1=HMAC-SHA256(secret, "<ts>.<nonce>." + body)`.
* Each request also carries `Idempotency-Key: <run|delivery>:<attempt>` and `traceparent`.
* Each workflow's first node, **Verify JT-Code signature**, checks the
  signature, the timestamp window and nonce reuse. A bad request gets **401**
  before any work runs.

**n8n → Django.** Callbacks use the same scheme with `N8N_WEBHOOK_SECRET` (or
`N8N_SENTRY_RELAY_SECRET` for the error relay). Django checks, in order:

1. **Signature, timestamp and nonce:** a forged or stale request gets 401, a
   replay gets 409, and a missing secret gets 503 (fail closed). Every
   rejection is audited and counted in `jt_security_events_total`.
2. **Durable dedupe:** each accepted nonce is stored in `WorkflowCallback`, so
   a resend is a no-op even after the replay cache is lost.
3. **State binding:**
   * A run callback must name the run's current `attempt`; a superseded
     attempt gets 409.
   * A terminal job cannot change again.
   * A document push must belong to an open sync delivery for that source.
4. **Canonical transitions:** job state, results and credit settlement go
   through `apps.jobs.transitions.apply_status_update`.

The secrets in each direction are different. The previous callback secret
(`N8N_WEBHOOK_SECRET_PREVIOUS`) remains valid while you rotate it.

## Retries

| Failure | Behaviour |
| --- | --- |
| Dispatch error: network, timeout, 404 (webhook not active), 408/425/429 or 5xx | Retry with exponential backoff (`N8N_RETRY_BASE_SECONDS` … `N8N_RETRY_MAX_SECONDS`) |
| Dispatch rejected (other 4xx) | Fail at once |
| `failed` callback with `retryable: true` (the default) | New attempt after backoff |
| Execution silent past the workflow's `timeoutSeconds` | `sweep_workflows` records `N8N_TIMEOUT` and retries |
| n8n execution `error`/`crashed`, or missing (`reconcile_n8n_executions`) | Failed attempt, then retry |
| Error workflow reports an execution | Failed attempt for the matching run or delivery, then retry |
| Attempts exhausted (`maxAttempts`) | Job `FAILED`: credit hold released, terminal callback and `jobs.job.failed` event |

Inside n8n, the HTTP nodes also retry up to 3–5 times. Every attempt uses the
same `runId`/`deliveryId`, so workflows can stay idempotent.

`sweep_workflows` runs every minute. It sends due attempts, expires silent
executions, and releases deliveries left behind by a worker that crashed
mid-request.

## Events (Kafka)

Every state change is written to the transactional outbox, which delivers it to Kafka:

* `orchestration.workflow.dispatched`
* `orchestration.workflow.progress`
* `orchestration.workflow.step`
* `orchestration.workflow.retry_scheduled`
* `orchestration.workflow.completed`
* `orchestration.workflow.failed`
* `orchestration.workflow.error`
* `orchestration.delivery.accepted`
* `orchestration.delivery.retry_scheduled`
* `orchestration.delivery.completed`
* `orchestration.delivery.failed`
* `knowledge.integration.sync_requested`
* `orchestration.n8n.<name>`, published by workflows themselves

Workflows may not subscribe to `orchestration.*` events, which prevents feedback loops.

Event deliveries are created in the **same transaction** as the domain event.
A delivery therefore exists exactly when the change commits, even if Kafka is
down.

## Versioned definitions

`n8n/workflows/<key>.v<version>.json` holds an n8n export plus `meta.jtCode`:

* `kind`
* `webhookPath`
* `taskTypes` / `eventTypes`
* `timeoutSeconds`
* `maxAttempts`
* `requiredCredentials`

`manage.py n8n_workflows validate` enforces the contract:

* Names and paths follow the version.
* The webhook answers from a Respond node.
* The signature is verified first.
* Callbacks are signed.
* Credentials are `${N8N_CREDENTIAL_*}` placeholders.
* Successful executions are not stored.
* A task type is claimed by only one workflow.

Definitions are registered on every `migrate` and are immutable per version.
`push` deploys them and `check` detects drift.

## APIs

| Endpoint | Who | Purpose |
| --- | --- | --- |
| `GET/POST/PATCH/DELETE /automations/`, `POST /automations/{id}/run/` | Tenant members (writers can change) | Scheduled automations: `webhook`, `slack_message`, or `email` to organization members only |
| `GET/POST /integrations/`, `POST /integrations/connect/`, `PATCH/DELETE /integrations/{id}/`, `POST /integrations/{id}/test/`, `/reconnect/`, `/sync/` | Tenant | The frontend's integrations contract; connection checks run through n8n |
| `GET /n8n/workflows/`, `GET /n8n/deliveries/` | Staff | Registered versions, sync state and delivery history |
| `/n8n/runs/…`, `/n8n/deliveries/{id}/status/`, `/n8n/knowledge/sources/{id}/documents/`, `/n8n/events/`, `/n8n/errors/` | n8n (signed) | Callbacks |

Knowledge sources of type `integration` take
`config: {"integrationId": "<integration id>", "acl": …}`.
