"""Durable evidence for agent runs that call ``knowledge.search``.

The agent engine binds the executing run for the duration of a graph
execution; the search tool then appends each result it shows the model as a
``Citation`` numbered after any evidence already recorded for that run, so the
``[n]`` markers in the final answer resolve to stored chunks even across
approval pauses and resumptions.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from django.db import transaction
from django.db.models import Max

_current_run: ContextVar[Any] = ContextVar("knowledge_evidence_run", default=None)


@contextmanager
def agent_run_evidence(run: Any) -> Iterator[None]:
    token = _current_run.set(run)
    try:
        yield
    finally:
        _current_run.reset(token)


def record_agent_evidence(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Number ``results`` for the model and persist them on the active agent run."""
    from apps.agents.models import AgentRun
    from apps.knowledge.access import accessible_chunks
    from apps.knowledge.models import Chunk, Citation

    run = _current_run.get()
    if run is None:
        return [{**item, "citation_index": index} for index, item in enumerate(results, start=1)]
    with transaction.atomic():
        AgentRun.objects.select_for_update().filter(id=run.id).first()
        start = (Citation.objects.filter(agent_run=run).aggregate(top=Max("citation_index"))["top"] or 0) + 1
        allowed = {
            str(chunk.id): chunk
            for chunk in accessible_chunks(
                Chunk.objects.filter(id__in=[item["chunk_id"] for item in results]),
                run.user,
                organization_id=run.organization_id,
            ).select_related("document")
        }
        numbered: list[dict[str, Any]] = []
        rows: list[Citation] = []
        for item in results:
            chunk = allowed.get(str(item["chunk_id"]))
            if chunk is None:
                continue
            index = start + len(numbered)
            numbered.append({**item, "citation_index": index})
            rows.append(
                Citation(
                    agent_run=run,
                    job_id=run.job_id,
                    chunk=chunk,
                    document=chunk.document,
                    relevance_score=float(item.get("rerank_score", item.get("score", 0.0))),
                    citation_index=index,
                    snippet=chunk.content[:1000],
                )
            )
        Citation.objects.bulk_create(rows)
    return numbered
