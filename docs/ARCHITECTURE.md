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

## Agentic RAG and vectors (Supabase pgvector)

Semantic retrieval uses the `vector` extension inside Supabase PostgreSQL rather than a standalone vector database. The `Chunk.embedding` column (fixed width per `VECTOR_EMBEDDING_DIMENSIONS`) stores provider-generated vectors next to the content they were derived from, so metadata and authorization filters reuse normal Django querysets. Tenant isolation is enforced by verified `collection_ids` plus a `collection__organization_id` filter re-checked inside the vector store before any distance calculation. Indexing runs as a Celery task (extract → chunk → embed → upsert) and emits `knowledge.document.indexed` outbox events; re-indexing is exposed through `POST /knowledge/documents/{id}/reindex/`. The `knowledge` migrations enable `CREATE EXTENSION IF NOT EXISTS vector` and an HNSW cosine index only on PostgreSQL, so the SQLite test database remains usable.
