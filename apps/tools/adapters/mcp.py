"""MCP (Model Context Protocol) client over Streamable HTTP, and tenant MCP tool specs.

Registered servers are discovered with ``initialize`` + ``tools/list``; their
tools are exposed as ``mcp.<server>.<tool>`` **only** once an administrator
allowlists them, and are treated as side-effecting (approval required) unless
the administrator also marks them read-only. Server-provided annotations such
as ``readOnlyHint`` are not trusted. All traffic uses the SSRF-safe egress path.
"""

from __future__ import annotations

import json
import re
from itertools import count
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.tools.crypto import decrypt_secret
from apps.tools.egress import safe_request
from apps.tools.gateway import ToolExecutionError
from apps.tools.models import McpServer
from apps.tools.registry import ToolContext, ToolSpec

PROTOCOL_VERSION = "2025-06-18"


def runtime_name(server_slug: str, tool_name: str) -> str:
    slug = re.sub(r"[^a-z0-9_]", "_", server_slug.lower()).strip("_") or "server"
    tool = re.sub(r"[^a-z0-9_]", "_", tool_name.lower()).strip("_") or "tool"
    if not tool[0].isalpha():
        tool = f"t_{tool}"
    if not slug[0].isalpha():
        slug = f"s_{slug}"
    return f"mcp.{slug}.{tool}".replace("__", "_")


class McpClient:
    """Minimal MCP Streamable-HTTP client (JSON or SSE responses)."""

    def __init__(self, server: McpServer):
        self.server = server
        self.session_id = ""
        self._ids = count(1)
        self.headers: dict[str, str] = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        credential = server.credential
        if credential is not None and credential.is_active and credential.encrypted_secret:
            header = str(credential.metadata.get("auth_header") or "Authorization")
            secret = decrypt_secret(credential.encrypted_secret)
            self.headers[header] = f"Bearer {secret}" if header.lower() == "authorization" else secret

    def _post(self, payload: dict[str, Any]) -> Any:
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        response = safe_request(
            "POST", self.server.url, headers=headers, json=payload, timeout=settings.MCP_TOOL_TIMEOUT_SECONDS
        )
        if session := response.headers.get("mcp-session-id"):
            self.session_id = session
        if response.status_code == 202 and "id" not in payload:
            return None
        if response.status_code >= 400:
            raise ToolExecutionError(f"MCP server returned HTTP {response.status_code}.")
        content_type = response.headers.get("content-type", "")
        messages: list[Any]
        if content_type.startswith("text/event-stream"):
            messages = []
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    try:
                        messages.append(json.loads(line[5:].strip()))
                    except json.JSONDecodeError as exc:
                        raise ToolExecutionError("MCP server sent a malformed event.") from exc
        else:
            messages = [response.json()]
        for message in messages:
            if isinstance(message, dict) and message.get("id") == payload.get("id"):
                if "error" in message:
                    error = message["error"] or {}
                    raise ToolExecutionError(
                        f"MCP error {error.get('code')}: {str(error.get('message'))[:200]}"
                    )
                return message.get("result")
        raise ToolExecutionError("MCP server returned no matching response.")

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}})

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "jt-code", "version": "1.0"},
            },
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        for _page in range(10):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {}) or {}
            tools.extend(tool for tool in result.get("tools", []) if isinstance(tool, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = self.request("tools/call", {"name": name, "arguments": arguments}) or {}
        text = (
            "\n".join(
                str(item.get("text", "")) for item in result.get("content", []) if isinstance(item, dict)
            )
            or json.dumps(result.get("structuredContent", {}))[:8000]
        )
        if result.get("isError"):
            raise ToolExecutionError(f"MCP tool reported an error: {text[:500]}")
        return text


def discover(server: McpServer) -> McpServer:
    """Refresh ``discovered_tools``; allowlists are left untouched."""
    try:
        client = McpClient(server)
        client.initialize()
        tools = client.list_tools()
    except (ToolExecutionError, PermissionError, RuntimeError) as exc:
        server.status = McpServer.Status.ERROR
        server.last_error = str(exc)[:2000]
        server.save(update_fields=["status", "last_error", "updated_at"])
        return server
    server.discovered_tools = [
        {
            "name": str(tool.get("name", ""))[:200],
            "description": str(tool.get("description", ""))[:1000],
            "inputSchema": tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {},
        }
        for tool in tools
        if tool.get("name")
    ][:200]
    server.status = McpServer.Status.ACTIVE
    server.last_error = ""
    server.last_discovered_at = timezone.now()
    server.save(
        update_fields=["discovered_tools", "status", "last_error", "last_discovered_at", "updated_at"]
    )
    return server


def _spec(server: McpServer, tool: dict[str, Any]) -> ToolSpec:
    original = tool["name"]

    def handler(ctx: ToolContext, arguments: dict[str, Any]) -> str:
        fresh = McpServer.objects.select_related("credential").get(id=server.id)
        if fresh.status != McpServer.Status.ACTIVE or original not in fresh.allowed_tools:
            raise ToolExecutionError("This MCP tool is no longer allowlisted.")
        client = McpClient(fresh)
        client.initialize()
        return client.call_tool(original, arguments)

    schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": {}}
    return ToolSpec(
        name=runtime_name(server.slug, original),
        description=f"[MCP {server.name}] {tool.get('description', '')}"[:1000],
        parameters=schema,
        handler=handler,
        side_effect=original not in (server.read_only_tools or []),
        timeout_seconds=float(settings.MCP_TOOL_TIMEOUT_SECONDS) + 5,
        default_enabled=True,
        strict_schema=False,
    )


def mcp_specs(organization_id: Any) -> dict[str, ToolSpec]:
    if not settings.ENABLE_MCP or organization_id is None:
        return {}
    specs: dict[str, ToolSpec] = {}
    servers = McpServer.objects.select_related("credential").filter(
        organization_id=organization_id, status=McpServer.Status.ACTIVE
    )
    for server in servers:
        allowed = set(server.allowed_tools or [])
        for tool in server.discovered_tools or []:
            if tool.get("name") in allowed:
                spec = _spec(server, tool)
                specs[spec.name] = spec
    return specs


def mcp_spec(name: str, organization_id: Any) -> ToolSpec | None:
    return mcp_specs(organization_id).get(name)


def register() -> None:
    """MCP tools are resolved per tenant at call time; nothing is registered statically."""
