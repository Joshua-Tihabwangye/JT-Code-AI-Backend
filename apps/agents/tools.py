"""Server-registered agent tools.

Tools invoked by the agent runtime live here and are the *only* way agents can
touch system functionality. Client-supplied tool names are whitelisted against
this registry, so clients can never define arbitrary callable behavior.

Every tool handler receives the acting ``user`` and ``organization_id`` so it
can enforce tenant scoping itself (e.g. retrieval is always filtered to the
organization's collections).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Tool:
    """A registered, invokable agent tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str]

    def tool_def(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


_REGISTRY: dict[str, Tool] = {}


def register_tool(tool: Tool) -> Tool:
    _REGISTRY[tool.name] = tool
    return tool


def get_tool(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def resolve_tools(names: Iterable[str]) -> list[Tool]:
    """Return registered tools for ``names``, preserving caller order."""
    wanted = set(names)
    return [tool for name, tool in _REGISTRY.items() if name in wanted]


def tool_defs(names: Iterable[str]) -> list[dict[str, Any]]:
    return [tool.tool_def() for tool in resolve_tools(names)]


def invoke_tool(name: str, *, user: Any, organization_id: Any, arguments: dict[str, Any]) -> str:
    """Invoke ``name`` with ``arguments`` bounded to ``user``/``organization_id``.

    Returns a string result (or an error description) so a failed tool call can
    feed back into the agent loop instead of aborting it.
    """
    tool = _REGISTRY.get(name)
    if tool is None:
        return f"Tool {name!r} is not registered."
    try:
        return tool.handler(user=user, organization_id=organization_id, **arguments)
    except TypeError as exc:
        return f"Tool {name!r} called with invalid arguments: {exc}"
    except Exception as exc:  # noqa: BLE001 - tool failures are surfaced to the model
        return f"Tool {name!r} execution failed: {exc}"


def _knowledge_search(user: Any, organization_id: Any, *, query: str = "", top_k: int = 5) -> str:
    if not query:
        return "A query is required for knowledge.search."
    top_k = min(max(int(top_k or 5), 1), 10)
    from apps.knowledge.evidence import record_agent_evidence
    from apps.knowledge.models import Collection
    from apps.knowledge.retrieval import embed_query_or_none, hybrid_retrieve

    collection_ids = list(
        Collection.objects.filter(organization_id=organization_id, is_active=True).values_list(
            "id", flat=True
        )
    )
    if not collection_ids:
        return "No knowledge collections exist for this organization."
    query_vector, _reason = embed_query_or_none(query)
    retrieval = hybrid_retrieve(
        query,
        query_vector,
        collection_ids=collection_ids,
        organization_id=organization_id,
        user=user,
        top_k=top_k,
    )
    if not retrieval.results:
        return "No results found matching the query."
    evidence = record_agent_evidence(retrieval.results)
    lines = ["Cite evidence by its number in square brackets, for example [1]."]
    for item in evidence:
        location = f"chunk {item.get('chunk_index', '?')}"
        if item.get("page_number"):
            location += f", page {item['page_number']}"
        lines.append(
            f"[{item['citation_index']}] {item.get('document_title', 'Document')} ({location}): "
            f"{item['content'][:1000]}"
        )
    return "\n".join(lines)


def _system_now(user: Any, organization_id: Any) -> str:  # noqa: ARG001
    from django.utils import timezone

    return f"Current UTC time: {timezone.now().isoformat(timespec='seconds')}."


def _whoami(user: Any, organization_id: Any) -> str:  # noqa: ARG001
    name = getattr(user, "display_name", "") or getattr(user, "full_name", "") or ""
    email = getattr(user, "email", "") or ""
    return f"User: {name or 'unknown'}; email: {email or '(none)'}."


register_tool(
    Tool(
        name="knowledge.search",
        description=(
            "Search the organization knowledge base for chunks related to a query "
            "and return the most relevant passages with source document titles."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "top_k": {"type": "integer", "description": "Max results (1-10).", "default": 5},
            },
            "required": ["query"],
        },
        handler=_knowledge_search,
    )
)
register_tool(
    Tool(
        name="system.now",
        description="Return the current UTC date and time.",
        parameters={"type": "object", "properties": {}},
        handler=_system_now,
    )
)
register_tool(
    Tool(
        name="identity.whoami",
        description="Return the acting user's display name and email.",
        parameters={"type": "object", "properties": {}},
        handler=_whoami,
    )
)


def default_agent_tools() -> tuple[str, ...]:
    """Tool names enabled for general research-style agent runs."""
    return ("knowledge.search", "system.now", "identity.whoami")
