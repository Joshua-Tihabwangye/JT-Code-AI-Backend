"""Google Gemini adapter over the Generative Language REST API.

Uses ``models/{model}:generateContent`` (and ``:streamGenerateContent?alt=sse``)
directly through ``httpx`` so timeouts, status codes and ``Retry-After`` are
under gateway control. Normalizes roles, system instructions, function calling,
safety blocks, finish reasons and token usage.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from django.conf import settings

from apps.ai_gateway.adapters import ChatMessage, GenerationResult, ToolCall, Usage
from apps.ai_gateway.providers.base import (
    ContentBlocked,
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

PROVIDER = "gemini"
_SAFETY_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
)


def _base_url(model: object) -> str:
    provider = getattr(model, "provider", None)
    return (getattr(provider, "base_url", "") or settings.GEMINI_API_BASE).rstrip("/")


def build_request(
    messages: list[ChatMessage],
    *,
    temperature: float,
    max_tokens: int | None,
    tools: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Translate normalized messages into a Gemini ``generateContent`` body."""
    system_parts: list[dict[str, str]] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = message.role
        if role == "system":
            system_parts.append({"text": message.content})
            continue
        if role == "tool":
            name = encode_tool_name(getattr(message, "name", "") or "tool")
            contents.append(
                {
                    "role": "user",
                    "parts": [{"functionResponse": {"name": name, "response": {"content": message.content}}}],
                }
            )
            continue
        parts: list[dict[str, Any]] = []
        if message.content:
            parts.append({"text": message.content})
        for call in getattr(message, "tool_calls", ()) or ():
            parts.append({"functionCall": {"name": encode_tool_name(call.name), "args": call.arguments}})
        contents.append(
            {"role": "model" if role == "assistant" else "user", "parts": parts or [{"text": ""}]}
        )

    generation_config: dict[str, Any] = {"temperature": temperature}
    if max_tokens is not None:
        generation_config["maxOutputTokens"] = max_tokens
    body: dict[str, Any] = {
        "contents": contents,
        "generationConfig": generation_config,
        "safetySettings": [
            {"category": category, "threshold": settings.GEMINI_SAFETY_THRESHOLD}
            for category in _SAFETY_CATEGORIES
        ],
    }
    if system_parts:
        body["systemInstruction"] = {"parts": system_parts}
    if tools:
        body["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": encode_tool_name(tool["name"]),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                    }
                    for tool in tools
                ]
            }
        ]
    return body


def _usage(metadata: dict[str, Any] | None) -> Usage:
    metadata = metadata or {}
    return Usage(
        input_tokens=int(metadata.get("promptTokenCount") or 0),
        output_tokens=int(metadata.get("candidatesTokenCount") or 0)
        + int(metadata.get("thoughtsTokenCount") or 0),
        cached_tokens=int(metadata.get("cachedContentTokenCount") or 0),
    )


def _parse_candidate(body: dict[str, Any], *, final: bool) -> tuple[str, tuple[ToolCall, ...], str]:
    """Return text, tool calls and finish reason; raise on a safety block."""
    feedback = body.get("promptFeedback") or {}
    if block_reason := feedback.get("blockReason"):
        raise ContentBlocked(f"Gemini blocked the prompt ({block_reason}).")
    candidates = body.get("candidates") or []
    if not candidates:
        if final:
            raise InvalidProviderResponse("Gemini returned no candidates.")
        return "", (), ""
    candidate = candidates[0]
    finish = normalize_finish_reason(candidate.get("finishReason")) if candidate.get("finishReason") else ""
    parts = (candidate.get("content") or {}).get("parts") or []
    texts: list[str] = []
    calls: list[ToolCall] = []
    for index, part in enumerate(parts):
        if "text" in part and not part.get("thought"):
            texts.append(str(part["text"]))
        if call := part.get("functionCall"):
            calls.append(
                ToolCall(
                    id=f"gemini-call-{index}",
                    name=decode_tool_name(str(call.get("name", ""))),
                    arguments=dict(call.get("args") or {}),
                )
            )
    text = "".join(texts)
    if finish == "content_filter" and not text and not calls:
        raise ContentBlocked("Gemini blocked the response.")
    if calls and finish in {"", "stop"}:
        finish = "tool_calls"
    return text, tuple(calls), finish


class GeminiChatAdapter:
    """Gemini ``generate``/``stream`` implementation of the gateway ``ChatAdapter``."""

    def _endpoint(self, model: object, method: str) -> tuple[str, dict[str, str], float]:
        provider = getattr(model, "provider", None)
        api_key = resolve_api_key(provider, "GEMINI_API_KEY")
        url = f"{_base_url(model)}/models/{provider_model_name(model)}:{method}"
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        return url, headers, request_timeout(provider)

    def generate(
        self,
        *,
        messages: list[ChatMessage],
        model: object,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> GenerationResult:
        url, headers, timeout = self._endpoint(model, "generateContent")
        body = post_json(
            url,
            headers=headers,
            payload=build_request(messages, temperature=temperature, max_tokens=max_tokens, tools=tools),
            timeout=timeout,
            provider=PROVIDER,
        )
        text, calls, finish = _parse_candidate(body, final=True)
        return GenerationResult(
            content=text,
            model_name=provider_model_name(model),
            provider_type="google",
            usage=_usage(body.get("usageMetadata")),
            finish_reason=finish or "stop",
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
        url, headers, timeout = self._endpoint(model, "streamGenerateContent")
        payload = build_request(messages, temperature=temperature, max_tokens=max_tokens, tools=tools)
        usage: Usage | None = None
        for event in stream_sse(
            f"{url}?alt=sse", headers=headers, payload=payload, timeout=timeout, provider=PROVIDER
        ):
            text, calls, finish = _parse_candidate(event, final=False)
            if event.get("usageMetadata"):
                usage = _usage(event["usageMetadata"])
            if text or calls or finish:
                yield StreamChunk(delta=text, tool_calls=calls, finish_reason=finish)
        yield StreamChunk(usage=usage or Usage(), finish_reason="")
