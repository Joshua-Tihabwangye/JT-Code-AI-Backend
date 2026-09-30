"""Llama adapter for hosted or self-hosted OpenAI-compatible chat endpoints.

Most Llama serving stacks (vLLM, Together, Groq, Fireworks, Ollama, llama.cpp
server) expose ``POST {base}/chat/completions`` with the OpenAI request and
response schema. The endpoint is ``Provider.base_url`` or ``LLAMA_API_BASE``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings

from apps.ai_gateway.adapters import AIProviderNotConfigured, ChatMessage, GenerationResult, ToolCall, Usage
from apps.ai_gateway.providers.base import (
    InvalidProviderResponse,
    StreamChunk,
    decode_tool_name,
    encode_tool_name,
    normalize_finish_reason,
    post_json,
    provider_model_name,
    request_timeout,
    resolve_api_key,
    stream_sse,
)

PROVIDER = "llama"


def _base_url(model: object) -> str:
    provider = getattr(model, "provider", None)
    base = (getattr(provider, "base_url", "") or settings.LLAMA_API_BASE).rstrip("/")
    if not base:
        raise AIProviderNotConfigured("LLAMA_API_BASE is not configured.")
    parsed = urlsplit(base)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local and settings.DEBUG):
        raise AIProviderNotConfigured(
            "Llama endpoints must use HTTPS (plain HTTP only for localhost in DEBUG)."
        )
    return base


def _message(message: ChatMessage) -> dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": getattr(message, "tool_call_id", "") or "call-0",
            "content": message.content,
        }
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    calls = getattr(message, "tool_calls", ()) or ()
    if calls:
        payload["tool_calls"] = [
            {
                "id": call.id or f"call-{index}",
                "type": "function",
                "function": {"name": encode_tool_name(call.name), "arguments": json.dumps(call.arguments)},
            }
            for index, call in enumerate(calls)
        ]
    return payload


def build_request(
    messages: list[ChatMessage],
    *,
    model_name: str,
    temperature: float,
    max_tokens: int | None,
    tools: list[dict[str, Any]] | None,
    stream: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model_name,
        "messages": [_message(message) for message in messages],
        "temperature": temperature,
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": encode_tool_name(tool["name"]),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                },
            }
            for tool in tools
        ]
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    return body


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except TypeError, json.JSONDecodeError:
        return {"_raw": str(raw)}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


def _usage(raw: dict[str, Any] | None) -> Usage:
    raw = raw or {}
    details = raw.get("prompt_tokens_details") or {}
    return Usage(
        input_tokens=int(raw.get("prompt_tokens") or 0),
        output_tokens=int(raw.get("completion_tokens") or 0),
        cached_tokens=int(details.get("cached_tokens") or 0),
    )


class LlamaChatAdapter:
    """OpenAI-compatible ``generate``/``stream`` implementation for Llama models."""

    def _endpoint(self, model: object) -> tuple[str, dict[str, str], float]:
        provider = getattr(model, "provider", None)
        api_key = resolve_api_key(provider, "LLAMA_API_KEY")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        return f"{_base_url(model)}/chat/completions", headers, request_timeout(provider)

    def generate(
        self,
        *,
        messages: list[ChatMessage],
        model: object,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> GenerationResult:
        url, headers, timeout = self._endpoint(model)
        model_name = provider_model_name(model)
        body = post_json(
            url,
            headers=headers,
            payload=build_request(
                messages, model_name=model_name, temperature=temperature, max_tokens=max_tokens, tools=tools
            ),
            timeout=timeout,
            provider=PROVIDER,
        )
        choices = body.get("choices") or []
        if not choices:
            raise InvalidProviderResponse("Llama endpoint returned no choices.")
        choice = choices[0]
        message = choice.get("message") or {}
        calls = tuple(
            ToolCall(
                id=str(call.get("id") or f"call-{index}"),
                name=decode_tool_name(str((call.get("function") or {}).get("name", ""))),
                arguments=_arguments((call.get("function") or {}).get("arguments")),
            )
            for index, call in enumerate(message.get("tool_calls") or [])
        )
        return GenerationResult(
            content=str(message.get("content") or ""),
            model_name=model_name,
            provider_type="llama",
            usage=_usage(body.get("usage")),
            finish_reason=normalize_finish_reason(choice.get("finish_reason")),
            tool_calls=calls,
        )

    def stream(
        self,
        *,
        messages: list[ChatMessage],
        model: object,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> Iterator[StreamChunk]:
        url, headers, timeout = self._endpoint(model)
        payload = build_request(
            messages,
            model_name=provider_model_name(model),
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            stream=True,
        )
        # Tool-call fragments arrive split across events, keyed by index.
        pending: dict[int, dict[str, str]] = {}
        usage: Usage | None = None
        finish = ""
        for event in stream_sse(url, headers=headers, payload=payload, timeout=timeout, provider=PROVIDER):
            if event.get("usage"):
                usage = _usage(event["usage"])
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                for fragment in delta.get("tool_calls") or []:
                    index = int(fragment.get("index", 0))
                    slot = pending.setdefault(index, {"id": "", "name": "", "args": ""})
                    slot["id"] = fragment.get("id") or slot["id"]
                    function = fragment.get("function") or {}
                    slot["name"] += function.get("name") or ""
                    slot["args"] += function.get("arguments") or ""
                if choice.get("finish_reason"):
                    finish = normalize_finish_reason(choice["finish_reason"])
                if text := delta.get("content"):
                    yield StreamChunk(delta=str(text))
        calls = tuple(
            ToolCall(
                id=slot["id"] or f"call-{index}",
                name=decode_tool_name(slot["name"]),
                arguments=_arguments(slot["args"]),
            )
            for index, slot in sorted(pending.items())
        )
        yield StreamChunk(tool_calls=calls, usage=usage or Usage(), finish_reason=finish or "stop")
