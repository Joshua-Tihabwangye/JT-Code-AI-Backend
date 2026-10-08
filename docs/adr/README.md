# Architecture Decision Records

This directory records the architecture decisions for the JT-Code backend.
Each ADR (`.md`) is numbered, immutable once accepted, and superseded only by a
newer ADR that explicitly references it.

## Index

| ID | Title | Status |
|----|-------|--------|
| ADR-001 | [Supabase Auth and Django authorization boundary](ADR-001-supabase-auth-and-django-authorization.md) | Accepted |
| ADR-002 | [Supabase Storage asset architecture](ADR-002-supabase-storage-architecture.md) | Accepted |
| ADR-003 | [Agentic RAG vector store (Supabase pgvector)](ADR-003-agentic-rag-vector-store.md) | Accepted |
| ADR-004 | [AI gateway for Gemini/Llama provider access](ADR-004-ai-gateway.md) | Accepted |
| ADR-005 | [Kafka, Celery and n8n responsibility boundaries](ADR-005-kafka-celery-n8n-boundaries.md) | Accepted |
| ADR-006 | [Partition high-volume tables only on measured need](ADR-006-table-partitioning.md) | Accepted |

## Conventions

- Status values: `Proposed`, `Accepted`, `Deprecated`, `Superseded by ADR-NNN`.
- Every accepted decision must state its context, the decision, and consequences.
- ADRs are review/approval artifacts: an accepted ADR is the freeze point for
  that decision as referenced by the production backlog.
