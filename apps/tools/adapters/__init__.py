"""Integration adapters register their tool specs here (called from ``ToolsConfig.ready``)."""

from __future__ import annotations

_REGISTERED = False


def register_all() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    from apps.tools.adapters import github, http_api, mcp, slack, web

    for module in (web, github, slack, http_api, mcp):
        module.register()
    _REGISTERED = True
