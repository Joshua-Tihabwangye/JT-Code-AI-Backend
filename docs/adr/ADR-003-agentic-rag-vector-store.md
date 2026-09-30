# ADR-003: Agentic RAG vector store (Supabase pgvector)

- **Status:** Accepted
- **Date:** 2026-09-20
- **Supersedes:** the earlier Pinecone "dedicated vector database" proposal in
  the initial approved-architecture draft. This ADR freezes the decision that
  was implemented in code (git: "replaced the Qdrant with PG vector").
- **Related:** Production backlog Phase 0, Phase 10

## Context

Agentic RAG needs semantic retrieval with strict tenant isolation. The original
architecture draft proposed a dedicated vector database (Pinecone). During
implementation the team evaluated keeping vectors inside the Supabase
PostgreSQL instance the product already uses.

## Decision

- **Canonical vector store: the `vector` extension inside Supabase PostgreSQL**
  (`pgvector`), not a standalone Pinecone deployment.
  - The `knowledge.Chunk.embedding` column stores provider-generated vectors
    next to the content, dimensions fixed by `VECTOR_EMBEDDING_DIMENSIONS`,
    indexed with an HNSW cosine index (see
    `apps/knowledge/migrations/0003_add_pgvector_embeddings.py`).
  - Metadata and authorization filters reuse ordinary Django querysets; the
    vector search path re-checks `collection__organization_id` before any
    distance computation (`apps/knowledge/vectorstore.py`).
  - Indexing runs as a Celery task (extract → chunk → embed → upsert) and emits
    `knowledge.document.indexed` outbox events.
- If a future requirement (e.g., scale or hybrid needs beyond Postgres) makes a
  dedicated vector database necessary again, a new ADR supersedes this one and
  Phase 10 must be re-run through the adapter boundary.

### Backlog reconciliation

The original Phase 0 backlog item names "Pinecone Agentic RAG architecture".
This accepted ADR is the architecture freeze for the current repository: it
intentionally replaces that target with Supabase PostgreSQL + pgvector. The
Phase 0 exit criterion is therefore satisfied by documenting the decision,
making the implementation matrix show `supabase_postgresql_pgvector` as active,
and treating Pinecone as out of scope unless a future superseding ADR restores
it.

## Consequences

- **Positive:** single operational database; transactional integrity between
  documents and vectors; tenant isolation enforced by SQL; no extra service.
- **Negative:** vector capacity scales with the relational database; embedding
  and index maintenance share Postgres resources; pagination of vector results
  is less flexible than a purpose-built engine.
- **Action (Phase 10):** versioned embeddings, hybrid retrieval + reranking,
  tenant-filtered retrieval, citations/groundedness evaluation and retrieval
  regression tests.

## Verification

- `grep -ri "pgvector" apps docs` returns the active implementation;
  `apps/knowledge/vectorstore.py` filters by organization before distance.
- Migration is a safe no-op on non-PostgreSQL test databases.
