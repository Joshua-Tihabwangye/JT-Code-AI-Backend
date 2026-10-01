"""The tool registry: every callable tool, its schema and its permission metadata.

Integration adapters register :class:`ToolSpec` objects; the original built-in
agent tools (``apps.agents.tools``) are exposed as read-only specs, and each
tenant's allowlisted MCP tools are resolved dynamically. Nothing outside this
registry can ever be executed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

VIEWER = "viewer"
EDITOR = "editor"
ADMIN = "admin"
ROLE_RANK = {VIEWER: 0, EDITOR: 1, ADMIN: 2}
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}$")

Handler = Callable[["ToolContext", dict[str, Any]], str]


@dataclass(frozen=True)
class ToolContext:
    """What a handler may use: the caller, tenant, tenant policy config and trace."""

    user: Any
    organization_id: Any
    config: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    side_effect: bool | Callable[[dict[str, Any]], bool] = False
    min_role: str = VIEWER
    provider: str = ""  # ToolCredential.Provider required to run, if any
    # Per-request ceiling adapters pass to the egress layer.
    timeout_seconds: float = 20.0
    default_enabled: bool = False
    # Extra keys allowed beyond the declared properties (MCP servers own their schemas).
    strict_schema: bool = True

    def is_side_effect(self, arguments: dict[str, Any]) -> bool:
        return self.side_effect(arguments) if callable(self.side_effect) else bool(self.side_effect)

    def required_role(self, arguments: dict[str, Any]) -> str:
        # Side-effecting calls always need at least editor access.
        if self.is_side_effect(arguments) and ROLE_RANK[self.min_role] < ROLE_RANK[EDITOR]:
            return EDITOR
        return self.min_role

    def tool_def(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


_SPECS: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    if not TOOL_NAME.fullmatch(spec.name) or "__" in spec.name:
        raise ValueError(f"Invalid tool name {spec.name!r}; use lowercase dotted names without '__'.")
    _SPECS[spec.name] = spec
    return spec


def _builtin_specs() -> dict[str, ToolSpec]:
    """Expose ``apps.agents.tools`` built-ins as read-only, enabled-by-default specs."""
    from apps.agents.tools import _REGISTRY

    def adapter(tool: Any) -> Handler:
        def run(ctx: ToolContext, arguments: dict[str, Any]) -> str:
            return str(tool.handler(user=ctx.user, organization_id=ctx.organization_id, **arguments))

        return run

    return {
        name: ToolSpec(
            name=name,
            description=tool.description,
            parameters=tool.parameters,
            handler=adapter(tool),
            default_enabled=True,
            strict_schema=True,
        )
        for name, tool in _REGISTRY.items()
    }


def static_specs() -> dict[str, ToolSpec]:
    return {**_builtin_specs(), **_SPECS}


def get_spec(name: str, organization_id: Any = None) -> ToolSpec | None:
    """Resolve a static tool or, for ``mcp.*`` names, the tenant's allowlisted MCP tool."""
    if name.startswith("mcp.") and organization_id is not None:
        from apps.tools.adapters.mcp import mcp_spec

        return mcp_spec(name, organization_id)
    return static_specs().get(name)


def specs_for_organization(organization_id: Any) -> dict[str, ToolSpec]:
    from apps.tools.adapters.mcp import mcp_specs

    return {**static_specs(), **mcp_specs(organization_id)}
