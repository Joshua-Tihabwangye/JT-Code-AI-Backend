# ADR-004: AI gateway for Gemini/Llama provider access

- **Status:** Accepted
- **Date:** 2026-09-20
- **Related:** Production backlog Phase 0, Phase 7

## Context

Business logic used to call provider SDKs directly for chat, agent turns and
embeddings. The approved architecture requires a single, normalized AI gateway
so that models, providers and fallbacks can change without touching callers,
and so every generation is metered, policed and audited.

## Decision

- All model generation flows through `apps.ai_gateway` as a single policy
  boundary. Callers never import provider SDKs.
- **Provider interface** (`apps.ai_gateway.adapters`): a normalized `ChatAdapter`
  protocol with `GenerationResult`, `Usage` and `ToolCall` outputs. Providers
  register through the `ADAPTERS` map: `echo` (dev/test), `openai`
  (OpenAI and OpenAI-compatible Llama endpoints via `base_url`), `google`
  (Gemini). Additional providers (Llama hosted/self-hosted variants) are added
  by implementing the same protocol without API changes (Phase 7).
- **Model registry** (`apps.ai_gateway.models`): `Provider`, `Model`,
  `ModelPolicy`, `ModelRun`, `Prompt`, `Evaluation`. Policies select the primary
  model and ordered fallbacks per task type; `ModelRun` records per-attempt
  tokens, estimated USD cost, latency and fallback metadata.
- **Execution service** (`apps.ai_gateway.service`): walks the policy chain,
  applies timeout/retry/cost/latency constraints, records a `ModelRun` per
  attempt, and settles on whichever model succeeds.
- Agent turns and RAG query answers are produced by the same gateway, so
  routing, fallback and metering apply uniformly.
- Secrets (`OPENAI_API_KEY`, `GEMINI_API_KEY`, provider credentials) live only
  in the environment and are referenced by `credentials_ref`, never in code.

## Consequences

- **Positive:** provider swap requires no client-API change; single metering and
  audit point; cost/latency policy enforced centrally.
- **Negative:** a gateway adds an abstraction layer and a potential single
  point of failure, mitigated by the fallback chain and `AI_PROVIDER=echo`
  offline mode.
- **Action (Phase 7):** Gemini/Llama adapters, model aliases and a capability
  registry, circuit-breaker/fallback tests, and usage/latency/cost recording.

## Verification

- `apps/ai_gateway/` implements the boundary; `tests/test_ai_gateway_execution.py`
  and `tests/test_agents_runtime.py` prove the gateway is the only model path.