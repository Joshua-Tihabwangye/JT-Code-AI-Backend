# Architecture Decision Records

This directory records the architecture decisions for the JT-Code backend.
Each ADR (`.md`) is numbered, immutable once accepted, and superseded only by a
newer ADR that explicitly references it.

## Index

| ID | Title | Status |
|----|-------|--------|
| ADR-001 | Supabase Auth and Django authorization boundary | Accepted |
| ADR-002 | ImageKit asset architecture (removes Cloudinary) | Accepted |
| ADR-003 | Agentic RAG vector store (Supabase pgvector) | Accepted |
| ADR-004 | AI gateway for Gemini/Llama provider access | Accepted |
| ADR-005 | Kafka, Celery and n8n responsibility boundaries | Accepted |

## Conventions

- Status values: `Proposed`, `Accepted`, `Deprecated`, `Superseded by ADR-NNN`.
- Every accepted decision must state its context, the decision, and consequences.
- ADRs are review/approval artifacts: an accepted ADR is the freeze point for
  that decision as referenced by the production backlog.