"""Generic external API adapter bound to tenant-registered HTTP connections.

A tenant ``http`` credential declares ``base_url`` (HTTPS), ``allowed_methods``,
``allowed_path_prefixes`` and an optional ``auth_header`` whose value is the
encrypted secret. Calls can only reach those paths on that host; GET/HEAD are
read-only, every other method is side-effecting and needs human approval.
"""

from __future__ import annotations

import posixpath
from typing import Any
from urllib.parse import urlsplit

from apps.tools.credentials import credential_for
from apps.tools.egress import safe_request
from apps.tools.gateway import ToolDenied
from apps.tools.registry import ToolContext, ToolSpec
from apps.tools.registry import register as register_spec

READ_METHODS = frozenset({"GET", "HEAD"})
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")


def is_side_effect(arguments: dict[str, Any]) -> bool:
    return str(arguments.get("method", "GET")).upper() not in READ_METHODS


def _normalized_path(path: str) -> str:
    if "://" in path or path.startswith("//") or "\\" in path or "\x00" in path:
        raise ToolDenied("INVALID_PATH", "path must be a relative API path.")
    # Reject traversal outright rather than silently normalizing it away.
    if ".." in path.split("/") or "%2e%2e" in path.lower():
        raise ToolDenied("INVALID_PATH", "path traversal is not allowed.")
    normalized = posixpath.normpath("/" + path.lstrip("/"))
    return normalized


def request(ctx: ToolContext, arguments: dict[str, Any]) -> str:
    credential, secret = credential_for(ctx.organization_id, "http", arguments["connection"])
    meta = credential.metadata
    base = str(meta.get("base_url", "")).rstrip("/")
    base_parts = urlsplit(base)
    if base_parts.scheme != "https" or not base_parts.hostname:
        raise ToolDenied("NOT_CONFIGURED", "The connection needs an https base_url.")
    method = str(arguments.get("method", "GET")).upper()
    if method not in {str(m).upper() for m in meta.get("allowed_methods", ["GET"])}:
        raise ToolDenied("METHOD_NOT_ALLOWED", f"{method} is not allowed on connection {credential.name!r}.")
    path = _normalized_path(arguments["path"])
    prefixes = [_normalized_path(str(prefix)) for prefix in meta.get("allowed_path_prefixes", ["/"])]
    if not any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes):
        raise ToolDenied("PATH_NOT_ALLOWED", f"{path} is outside the connection's allowed paths.")
    headers = {"Accept": "application/json"}
    if header := meta.get("auth_header"):
        headers[str(header)] = secret
    response = safe_request(
        method,
        f"{base}{path}",
        params=arguments.get("query") or None,
        json=arguments.get("body") if method not in READ_METHODS else None,
        headers=headers,
        allowed_hosts=[base_parts.hostname],
    )
    return f"HTTP {response.status_code}\n{response.text[:8000]}"


def register() -> None:
    register_spec(
        ToolSpec(
            name="http.request",
            description=(
                "Call an organization-registered external API connection. GET/HEAD read data; "
                "other methods change data and require approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "connection": {"type": "string", "pattern": "^[a-z0-9-_]{1,64}$"},
                    "method": {"type": "string", "enum": list(METHODS)},
                    "path": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "query": {
                        "type": "object",
                        "additionalProperties": {"type": ["string", "number", "boolean"]},
                    },
                    "body": {"type": ["object", "array"]},
                },
                "required": ["connection", "path"],
            },
            handler=request,
            side_effect=is_side_effect,
            provider="http",
        )
    )
