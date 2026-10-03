# JT-Code backend architecture

## Source-of-truth boundary

Django and Supabase-hosted PostgreSQL own users' local application profile mappings, conversations, job state, asset metadata, usage and audit references. Supabase Auth owns authentication sessions and primary identity. ImageKit owns asset bytes and transformations. Redis provides caching and Celery transport; it is not durable business state. Kafka carries integration/domain events; events are emitted through the PostgreSQL transactional outbox.

## Request path

1. React or React Native gets a Supabase session JWT.
2. Django verifies the JWT signature, expiry, audience when configured, and authorized party.
3. Django enforces ownership and policy against PostgreSQL.
4. A mutating request writes canonical state and an outbox row in one transaction.
5. Celery handles background execution through Redis.
6. The outbox publisher sends committed events to Kafka.
7. Sentry receives sanitized errors and traces from Django, Celery, Kafka consumers and the n8n error relay.

## ImageKit boundary

The browser never receives the ImageKit private key. Django creates a single-use, tenant-bound upload intent and signs short-lived authentication parameters scoped to its unique private-file path. Completion verifies ImageKit metadata, downloads the object through a signed URL, and records a SHA-256 content checksum before registration. Django owns asset metadata and authorization; scheduled workers purge soft-deleted files, reconcile provider identity, and delete aged unregistered objects under the application upload root. Regulated deployments can add a malware-scanning approval state before changing an asset from quarantined to ready.

## Supabase PostgreSQL

Use a direct or session-pooler connection for long-running Django services. Require TLS in hosted environments. Supabase Auth owns user identity and session management; ImageKit owns asset bytes and transformations.

## AI gateway and job execution

All AI generation flows through `apps.ai_gateway` as a single policy boundary. Providers are wrapped by normalized adapters (Gemini REST, OpenAI-compatible Llama, and a deterministic `echo` backend for dev/test). Clients address model aliases; task `ModelPolicy` rows point at an alias whose ordered, capability-compatible targets form the fallback chain, guarded by a shared circuit breaker, bounded retries, a request deadline and cost ceilings (see `docs/AI_GATEWAY.md`); `apps.ai_gateway.service.generate_completion` walks the chain, records a `ModelRun` per attempt (tokens, estimated USD cost, latency, fallback metadata) and settles on whichever model succeeds. `GENERAL_QUESTION`, `RAG_QUERY` and `SEARCH_RESEARCH` jobs are executed internally by `apps.jobs.executor` (replacing the n8n placeholder for AI task types) and emit `jobs.job.completed` / `jobs.job.failed` outbox events with the same `request_id`/`trace_id`. `RAG_QUERY` jobs run the tenant-scoped pgvector retriever and inject the top chunks as grounded context into the gateway so answers carry `sources` and a `grounded` flag.

## Agent runtime (LangGraph)

`apps.agents` runs bounded LangGraph graphs (`direct_answer`, `research`) as durable, traced runs executed by Celery workers. An intent router picks the graph (deterministic rules, or the `classification` model alias with rules fallback); a tool-selection policy narrows tools to registered ∩ graph ∩ agent ∩ request; hard budgets (steps, model calls, tool calls, cost, wall-clock) are enforced before every model/tool call; an input safety gate blocks jailbreaks and tool output is wrapped and scanned as untrusted data. Graph state is checkpointed in PostgreSQL (`DjangoCheckpointSaver`) so a crashed run resumes without repeating completed steps. Every run records `AgentStep` traces, an `AgentEvaluation` and `agents.run.*` outbox events. `SEARCH_RESEARCH` jobs execute as durable agent runs. See `docs/AGENT_RUNTIME.md`.

## Tools, MCP and integrations

`apps.tools` is the only way agents or clients can act on external systems. A single gateway (`apps.tools.gateway.execute_tool`) enforces tool existence, tenant enablement, role, JSON-schema arguments, prompt-injection taint and human approval for side effects, then audits every call (`ToolInvocation`, `AuditEvent`). Adapters cover GitHub (App tokens minted per call for one repository and minimum permissions, branch/PR-first), Slack (encrypted bot tokens, channel allowlist), web fetch/search and a generic external-API connection, all through an SSRF-safe egress layer with DNS pinning; remote MCP servers are discovered over Streamable HTTP and their tools are callable only once allowlisted. Side-effecting calls pause agent runs (`waiting_approval`) until an editor/admin decides; the run then resumes from its checkpoint. See `docs/TOOLS_AND_MCP.md`.

## Agentic RAG and vectors (Supabase pgvector)

Semantic retrieval uses the `vector` extension inside Supabase PostgreSQL rather than a standalone vector database. The `Chunk.embedding` column (fixed width per `VECTOR_EMBEDDING_DIMENSIONS`) stores provider-generated vectors next to the content they were derived from, so metadata and authorization filters reuse normal Django querysets. Tenant isolation is enforced by verified `collection_ids` plus a `collection__organization_id` filter re-checked inside the vector store before any distance calculation. Indexing runs as a Celery task (extract → chunk → embed → upsert) and emits `knowledge.document.indexed` outbox events; re-indexing is exposed through `POST /knowledge/documents/{id}/reindex/`. The `knowledge` migrations enable `CREATE EXTENSION IF NOT EXISTS vector` and an HNSW cosine index; development, tests and production all run on Supabase PostgreSQL.
