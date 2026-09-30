# AI gateway and model registry (Phase 7)

Every model call goes through `apps.ai_gateway.service.generate_completion` (or
`stream_completion`). Callers never import provider SDKs, never name provider
models, and never see provider errors.

## Aliases, not models

Clients and task policies address **aliases**. An alias (`ModelAlias`) is an ordered list
of target models (`ModelAliasTarget.priority`, lowest first) plus the capabilities every
target must provide.

| Alias | Capabilities | Used by |
| --- | --- | --- |
| `default-chat` | chat | `GENERAL_QUESTION`, `RAG_QUERY`, chat requests |
| `tool-calling` | chat, tools | `SEARCH_RESEARCH` agent turns |
| `fast-chat` | chat | low-latency features |
| `classification` | chat, json_mode | intent routing (Phase 8) |

**Swapping providers** is an operator action with no client change: in Django admin → Model
aliases, re-order or replace the targets. `GET /api/v1/model-aliases/` and
`GET /api/v1/system/capabilities/` expose aliases and capabilities only, never provider
model names or credentials. `POST /api/v1/completion/` accepts `model_alias` and returns
`modelAlias`.

Gemini and Llama rows track `GEMINI_DEFAULT_MODEL` / `LLAMA_DEFAULT_MODEL` through
`Model.metadata.provider_model_setting`, so moving to a newer provider model is an
environment change. Keep `input/output_price_per_token` current in the admin: cost ceilings
and `ModelRun` costs depend on them.

## Providers

| Type | Adapter | Endpoint | Credentials |
| --- | --- | --- | --- |
| `google` | `providers.gemini.GeminiChatAdapter` | `GEMINI_API_BASE` `models/{m}:generateContent` (`:streamGenerateContent?alt=sse`) | `GEMINI_API_KEY` (`x-goog-api-key`) |
| `llama` | `providers.llama.LlamaChatAdapter` | `LLAMA_API_BASE/chat/completions` (any OpenAI-compatible server) | `LLAMA_API_KEY` (Bearer) |
| `echo` | `adapters.EchoChatAdapter` | none — dev/test only | forbidden when `AI_PROVIDER` ≠ `echo`; rejected in staging/production |

`Provider.credentials_ref` may only name a `GEMINI_*/LLAMA_*/OPENAI_*…API_KEY` variable, so an
admin-editable row can never read other secrets. Llama endpoints must be HTTPS (plain HTTP
only for `localhost` with `DEBUG`). Gemini safety settings use `GEMINI_SAFETY_THRESHOLD`
(production may not disable them). Tool names are encoded (`knowledge.search` →
`knowledge__search`) because provider function names forbid dots.

## Resilience policy

For each capability-compatible candidate, in alias order:

1. **Cost ceiling** — skip the model if its worst-case cost (prompt + max output) exceeds
   `min(AI_GATEWAY_MAX_COST_USD, policy.max_cost_usd)`. If every candidate is too expensive
   the request fails with `AI_BUDGET_EXCEEDED`.
2. **Circuit breaker** — per provider, shared through Redis. `circuit_breaker_threshold`
   consecutive health failures (timeout, 5xx, 429, auth, malformed response) open it for
   `AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS`; then one half-open trial call closes or re-opens
   it. A cache outage fails **open**.
3. **Retries** — retryable errors retry the same model up to
   `min(provider.max_retries, AI_MAX_RETRIES)` with exponential backoff and full jitter
   (`AI_RETRY_BASE_SECONDS`, capped at `AI_RETRY_MAX_BACKOFF_SECONDS`). `Retry-After` is
   honoured when it fits the cap; a longer hint moves straight to the next model.
4. **Deadline** — all retries and fallbacks share one `min(AI_GATEWAY_MAX_LATENCY_MS,
   policy.max_latency_ms)` budget; each HTTP call also has
   `min(provider.timeout_seconds, AI_REQUEST_TIMEOUT_SECONDS)`.
5. **Fallback** — move to the next compatible model, except for `CONTENT_BLOCKED` (safety
   filters are final; the gateway never shops for a less careful model).
   `AI_GATEWAY_FALLBACK_ENABLED=false` restricts a request to its first candidate.

Streaming falls back only before the first chunk reaches the caller; a mid-stream failure
is final.

## What is recorded

One `ModelRun` per gateway request: alias, final provider/model, tenant (`organization`),
`request_id`/`trace_id`/`job_id`, tokens, estimated cost, total latency, same-model
`retry_count`, `fallback_used`, and `metadata.attempts` — one entry per candidate with its
error code, retries and latency. `metadata.skipped` lists models excluded for missing
capabilities or unavailability.

## Operating it

- **Provider outage**: the breaker opens after the threshold and traffic flows to the next
  alias target automatically. Watch `ModelRun.metadata.attempts[*].error_code` and
  `fallback_used` rates. To force traffic away, set the provider `status` to `maintenance`.
- **Bad credentials**: attempts show `PROVIDER_AUTH_FAILED`; fix the environment variable and
  the breaker closes after its cooldown.
- **Provider change**: add the `Model`, set prices and capabilities, add it as a target of
  the relevant aliases, then remove the old target. No deploy is required.
