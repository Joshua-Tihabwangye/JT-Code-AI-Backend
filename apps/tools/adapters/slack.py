"""Slack adapter with tenant-scoped bot credentials and a channel allowlist.

The tenant's ``slack`` credential holds its bot token (encrypted at rest) and
``allowed_channels``; messages can only reach those channels, and broadcast
mentions are neutralized so an injected prompt cannot page a whole workspace.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings

from apps.tools.credentials import credential_for
from apps.tools.egress import safe_request
from apps.tools.gateway import ToolDenied, ToolExecutionError
from apps.tools.registry import EDITOR, ToolContext, ToolSpec
from apps.tools.registry import register as register_spec

_BROADCAST = re.compile(r"<!(channel|here|everyone)[^>]*>|@(channel|here|everyone)\b", re.IGNORECASE)


def _channels(ctx: ToolContext) -> tuple[list[str], str]:
    credential, token = credential_for(ctx.organization_id, "slack")
    if not token.startswith("xoxb-"):
        raise ToolDenied("NOT_CONFIGURED", "The Slack connection needs a bot token (xoxb-…).")
    return [str(channel) for channel in credential.metadata.get("allowed_channels", [])], token


def neutralize(text: str) -> str:
    return _BROADCAST.sub(lambda match: match.group(0).replace("@", "@​").replace("<!", "<​!"), text)


def list_channels(ctx: ToolContext, arguments: dict[str, Any]) -> str:
    channels, _token = _channels(ctx)
    return "Channels the agent may post to:\n" + "\n".join(f"- {channel}" for channel in channels)


def post_message(ctx: ToolContext, arguments: dict[str, Any]) -> str:
    channels, token = _channels(ctx)
    channel = arguments["channel"]
    if channel not in channels:
        raise ToolDenied(
            "CHANNEL_NOT_ALLOWED", f"Channel {channel!r} is not in this organization's allowlist."
        )
    response = safe_request(
        "POST",
        f"{settings.SLACK_API_BASE.rstrip('/')}/chat.postMessage",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        json={"channel": channel, "text": neutralize(arguments["text"]), "unfurl_links": False},
        allowed_hosts=[urlsplit(settings.SLACK_API_BASE).hostname or "slack.com"],
    )
    body = response.json() if response.content else {}
    if response.status_code >= 400 or not body.get("ok"):
        raise ToolExecutionError(f"Slack rejected the message: {body.get('error', response.status_code)}")
    return f"Posted to {channel} (ts {body.get('ts', '')})."


def register() -> None:
    register_spec(
        ToolSpec(
            name="slack.list_channels",
            description="List Slack channels this organization allows the agent to post to.",
            parameters={"type": "object", "properties": {}},
            handler=list_channels,
            provider="slack",
        )
    )
    register_spec(
        ToolSpec(
            name="slack.post_message",
            description="Post a message to an allowed Slack channel (requires approval).",
            parameters={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "minLength": 1, "maxLength": 80},
                    "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                },
                "required": ["channel", "text"],
            },
            handler=post_message,
            side_effect=True,
            min_role=EDITOR,
            provider="slack",
        )
    )
