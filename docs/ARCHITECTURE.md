# JT-Code backend architecture

## Source-of-truth boundary

Django and Supabase-hosted PostgreSQL own users' local application profile mappings, conversations, job state, asset metadata, usage and audit references. Supabase Auth owns authentication sessions and primary identity. Cloudinary owns asset bytes and transformations. Redis provides caching and Celery transport; it is not durable business state. Kafka carries integration/domain events; events are emitted through the PostgreSQL transactional outbox.

## Request path

1. React or React Native gets a Supabase session JWT.
2. Django verifies the JWT signature, expiry, audience when configured, and authorized party.
3. Django enforces ownership and policy against PostgreSQL.
4. A mutating request writes canonical state and an outbox row in one transaction.
5. Celery handles background execution through Redis.
6. The outbox publisher sends committed events to Kafka.
7. Sentry receives sanitized errors and traces from Django, Celery, Kafka consumers and the n8n error relay.

## Cloudinary boundary

The browser never receives the Cloudinary API secret. Django signs short-lived upload parameters scoped to the user's folder. The completion endpoint verifies the asset against Cloudinary before saving metadata. Add malware scanning/quarantine before allowing generated or uploaded files to become downloadable in regulated deployments.

## Supabase PostgreSQL

Use a direct or session-pooler connection for long-running Django services. Require TLS in hosted environments. Supabase Auth owns user identity and session management; Cloudinary owns asset bytes and transformations.

## AI gateway and job execution

All AI generation flows through `apps.ai_gateway` as a single policy boundary. Provider SDKs are wrapped by normalized adapters (OpenAI-compatible, Google Gemini, and a deterministic `echo` backend for dev/test). `ModelPolicy` rows choose the primary model and ordered fallbacks per task type; `apps.ai_gateway.service.generate_completion` walks the chain, records a `ModelRun` per attempt (tokens, estimated USD cost, latency, fallback metadata) and settles on whichever model succeeds. `GENERAL_QUESTION`, `RAG_QUERY` and `SEARCH_RESEARCH` jobs are executed internally by `apps.jobs.executor` (replacing the n8n placeholder for AI task types) and emit `jobs.job.completed` / `jobs.job.failed` outbox events with the same `request_id`/`trace_id`. `RAG_QUERY` jobs run the tenant-scoped pgvector retriever and inject the top chunks as grounded context into the gateway so answers carry `sources` and a `grounded` flag.

## Agent runtime (LangGraph)

`SEARCH_RESEARCH` jobs run a multi-step agent in `apps.agents` on top of the AI gateway. `apps.agents.tools` is a small registry of server-side tools (`knowledge.search` org-scoped pgvector retrieval, `system.now`, `identity.whoami`); handlers are invoked with the authorized `user` and `organization_id`, and failures are returned as strings rather than raised. `apps.agents.runtime.run_agent` builds a LangGraph state graph (`call_model` → route on tool calls → `execute_tools` → `call_model`) bounded by `AGENT_MAX_ITERATIONS`; each model turn goes through `generate_completion`, so provider routing, fallback and `ModelRun` metering apply uniformly. Tool invocations are appended to the conversation as `ToolMessage`s, keeping the agent loop self-correcting. `iter_agent` exposes the same loop as a streaming iterator (intermediate updates, then a `summary` event) as the base for future SSE streaming; `run_agent` drains it into a summary `AgentRun` carrying the final answer, `invoked_tools`, `model_runs` and token usage. A `SEARCH_RESEARCH` job result includes the answer, tool names, a serialized transcript and `grounded` = whether `knowledge.search` was invoked.

## Agentic RAG and vectors (Supabase pgvector)

Semantic retrieval uses the `vector` extension inside Supabase PostgreSQL rather than a standalone vector database. The `Chunk.embedding` column (fixed width per `VECTOR_EMBEDDING_DIMENSIONS`) stores provider-generated vectors next to the content they were derived from, so metadata and authorization filters reuse normal Django querysets. Tenant isolation is enforced by verified `collection_ids` plus a `collection__organization_id` filter re-checked inside the vector store before any distance calculation. Indexing runs as a Celery task (extract → chunk → embed → upsert) and emits `knowledge.document.indexed` outbox events; re-indexing is exposed through `POST /knowledge/documents/{id}/reindex/`. The `knowledge` migrations enable `CREATE EXTENSION IF NOT EXISTS vector` and an HNSW cosine index only on PostgreSQL, so the SQLite test database remains usable.
