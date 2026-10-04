# API v1 contract

The machine-readable API contract is served at `GET /api/v1/schema/`; Swagger UI is at
`GET /api/v1/docs/`. Clients must use `/api/v1/` and treat a new major path as the only
place for breaking API changes.

## Common conventions

- Authenticate with a Supabase bearer JWT. Tenant selection is server-authorized; clients
  may send `X-Organization-ID` only to select an organization they already belong to.
- `X-Request-ID` is accepted and returned. `X-Trace-ID` is also returned for tracing.
- Cursor lists return `next`, `previous`, and `results`. Use the opaque `cursor` value in
  the next request; do not construct or persist an interpretation of it. `pageSize` is
  optional and capped at 100.
- Duplicate-prone chat submission requires `Idempotency-Key`. Reusing a key with exactly
  the same request returns the existing request and `Idempotency-Replayed: true`. Reusing
  it for a changed request returns `409 idempotency_conflict`.
- DRF-raised errors use this envelope:

```json
{
  "code": "validation_error",
  "message": "Request validation failed.",
  "details": {"field": ["reason"]},
  "requestId": "client-or-server-request-id"
}
```

## Conversation endpoints

- `GET, POST /api/v1/conversations/` — cursor list/create. `q` filters titles;
  `includeArchived=true` includes archived conversations.
- `GET, PATCH, DELETE /api/v1/conversations/{id}/` — read/update/delete.
- `POST /api/v1/conversations/{id}/archive/` and `/unarchive/` — archive lifecycle.
- `GET /api/v1/conversations/{id}/messages/` — cursor list, newest first.
- `POST /api/v1/conversations/{id}/messages/` — submit `{ "content": "..." }` with
  `Idempotency-Key`; returns a `ChatRequest` with `202`, or `200` on replay.
- `GET, POST /api/v1/conversations/{id}/feedback/` — list the caller's feedback or create
  / update feedback. A `chatRequestId` makes feedback idempotent per caller and request.
- `GET, POST /api/v1/chat/requests/` — cursor list/filter by `conversationId` or `status`,
  or submit the backward-compatible `{ conversationId, chatInput }` request shape.
- `GET /api/v1/chat/requests/{id}/stream/` — authenticated SSE. Event names are `status`,
  `completed`, `failed`, `cancelled`, `heartbeat`, and `timeout`. The client must fall back
  to polling `GET /api/v1/chat/requests/{id}/` after a timeout or disconnect.

Conversation writes require editor or administrator access to the organization that owns the
conversation (not merely the caller's primary or header-selected organization). Posting to an
archived conversation returns `409 conversation_archived`; unarchive it first.

## Jobs

- `GET, POST /api/v1/jobs/` — list/create. `task_type` must be a worker-supported type. An
  unfunded wallet returns `402 insufficient_credits`.
- `GET /api/v1/jobs/{id}/`, `POST /api/v1/jobs/{id}/cancel/`, `POST /api/v1/jobs/{id}/retry/`.
  Jobs are not client-editable or deletable (`PUT`/`PATCH`/`DELETE` return 405); state changes
  only through cancel/retry, workers and the signed n8n status callback.

## Knowledge (Agentic RAG)

All knowledge endpoints live under `/api/v1/knowledge/` and use camelCase fields. The full
table is in [AGENTIC_RAG_DESIGN.md](AGENTIC_RAG_DESIGN.md#api-apiv1knowledge). Highlights:

- `GET, POST /api/v1/knowledge/collections/` — unpaginated list of collections, each with the
  sources the caller may see; create needs editor access. Embedding provider/model are
  server-configured and read-only; `organizationId` is never writable.
- `POST /api/v1/knowledge/collections/{id}/sources/` — add a `text`, `url` or `file` source and
  queue indexing; returns the updated collection.
- `GET /api/v1/knowledge/search/?query=&collectionId=` — hybrid search returning
  `KnowledgeSearchResult[]`; `X-Retrieval-Reranker` / `X-Retrieval-Degraded` report how it ran.
- `POST /api/v1/knowledge/query/` — synchronous grounded answer with citations (reserves and
  settles credits; `402 insufficient_credits` when the wallet cannot cover it).
- `POST /api/v1/knowledge/rag/query/` — the same as an asynchronous job.

## Files (ImageKit assets)

- `GET /api/v1/files/` — visible files as `FileItem[]` (newest 500; add `?page=` for a paginated
  envelope, `?q=` to filter by name). `POST` with multipart `file` uploads a private file.
- `GET, PATCH, DELETE /api/v1/files/{id}/` — read; rename / change `visibility`
  (`private` | `organization`, owner or admin only); soft delete (`409` while in use unless
  `?force=true`).
- `POST /api/v1/files/{id}/restore/`, `POST /api/v1/files/bulk-delete/` (`{ids, force}`),
  `GET /api/v1/files/{id}/download/` (streamed bytes), `POST /api/v1/files/{id}/access/`
  (short-lived signed URL), `POST /api/v1/files/{id}/attach/` (`{conversationId}`).
- Direct uploads: `POST /api/v1/files/signature/` returns an ImageKit V2 `token` plus the exact
  `uploadParams` to send to `uploadUrl`; then `POST /api/v1/files/complete/` with
  `uploadIntentId`, `uploadToken`, `fileId` and `filePath`.

## Usage, quotas and limits

See [USAGE_METERING.md](USAGE_METERING.md). Billable endpoints reserve credits before work and can
refuse with `402 insufficient_credits`, `402 spending_limit_reached`, `429 quota_exceeded` or
`429 concurrency_limit`; rate limits return `429` with `Retry-After`.

- `GET /api/v1/usage/` — current-period `totalCredits`, `byType`, `byFeature`, `quotas`, `spending`,
  `reservedCredits` and `concurrency` for the selected organization.
- `GET /api/v1/usage/records/?feature=&period=YYYY-MM` — immutable usage records.
- Staff only: `GET /api/v1/internal/usage/summary/`, `/internal/usage/organizations/`,
  `/internal/usage/reconciliations/`, `/internal/usage/reservations/`.

## Billing (Stripe)

See [BILLING_AND_STRIPE.md](BILLING_AND_STRIPE.md#api-frontend-contract). Plans, Checkout
(`POST /api/v1/plans/{slug}/subscribe/`), `GET /api/v1/subscriptions/` with
`POST /subscriptions/cancel/` and `/reactivate/`, `GET, PATCH /api/v1/wallets/me/`,
`POST /api/v1/wallets/me/topup/`, payment methods via SetupIntent, the Billing Portal, invoices and
`POST /api/v1/webhooks/stripe/` (signed, idempotent per Stripe event id).

## Inbound webhooks

`POST /api/v1/inbound-webhooks/{webhookId}/` (public, signature-authenticated). Send
`X-Webhook-Timestamp` (unix seconds) and `X-Webhook-Signature: sha256=<hex>` where the hex is
HMAC-SHA256 of `"<timestamp>.<raw body>"` with the webhook secret. Stale timestamps (>5 min),
bad signatures and replays are rejected.
All reads are tenant-scoped on the server; resource IDs never grant access by themselves.
