"""Normalized chat generation adapters for the AI gateway.

Adapters translate a provider API (Gemini, Llama/OpenAI-compatible, OpenAI)
into a common ``ChatAdapter`` boundary with ``generate`` and optional
``stream``. Consumers only ever talk to the gateway, so models, fallbacks and
providers can be reconfigured without touching callers.

An ``echo`` adapter (``AI_PROVIDER=echo``) is provided for local development
and the deterministic offline test suite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from django.conf import settings


class AIGatewayError(RuntimeError):
    """Base error for the AI gateway execution layer."""

    code = "AI_GATEWAY_ERROR"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class AIProviderNotConfigured(AIGatewayError):
    """The provider adapter cannot be used because credentials are missing."""

    code = "AI_PROVIDER_NOT_CONFIGURED"


class UnsupportedProvider(AIGatewayError):
    """No adapter exists for the given provider type."""

    code = "AI_PROVIDER_UNSUPPORTED"


@dataclass(frozen=True)
class ToolCall:
    """A function-calling invocation requested by the model."""

    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ChatMessage:
    """A single normalized conversation turn.

    Assistant turns may carry ``tool_calls``; ``tool`` turns carry the
    ``tool_call_id`` and tool ``name`` they answer, so providers receive
    structured function-calling history rather than flattened text.
    """

    role: str
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str = ""
    name: str = ""

    def to_openai(self) -> dict:
        if self.role == "tool":
            return {"role": "tool", "tool_call_id": self.tool_call_id or "call-0", "content": self.content}
        payload: dict = {"role": self.role, "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name.replace(".", "__"),
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in self.tool_calls
            ]
        return payload


@dataclass(frozen=True)
class Usage:
    """Token/asset usage metrics for a single generation call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    image_count: int = 0


@dataclass(frozen=True)
class GenerationResult:
    """Normalized output of a single adapter call."""

    content: str
    model_name: str
    provider_type: str
    usage: Usage
    finish_reason: str = "stop"
    tool_calls: tuple[ToolCall, ...] = ()


def estimate_tokens(text: str) -> int:
    """Deterministic ~4-characters-per-token estimate (for echo/cost display)."""
    return max(1, (len(text) + 3) // 4)


class ChatAdapter(Protocol):
    """Protocol implemented by every provider adapter."""

    def generate(
        self,
        *,
        messages: list[ChatMessage],
        model: object,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
    ) -> GenerationResult: ...


class EchoChatAdapter:
    """Deterministic, offline stand-in used when ``AI_PROVIDER=echo``."""

    def generate(
        self,
        *,
        messages: list[ChatMessage],
        model: object,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
    ) -> GenerationResult:
        if settings.AI_PROVIDER != "echo":
            raise AIProviderNotConfigured("EchoChatAdapter is only available when AI_PROVIDER=echo.")
        prompt = " | ".join(f"{m.role}: {m.content}" for m in messages)
        content = f"JT-Code development response: {prompt}"
        return GenerationResult(
            content=content,
            model_name=getattr(model, "name", "echo-chat"),
            provider_type="echo",
            usage=Usage(
                input_tokens=estimate_tokens(prompt),
                output_tokens=estimate_tokens(content),
            ),
        )

    def stream(
        self,
        *,
        messages: list[ChatMessage],
        model: object,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
    ):
        from apps.ai_gateway.providers.base import StreamChunk

        result = self.generate(
            messages=messages, model=model, temperature=temperature, max_tokens=max_tokens, tools=tools
        )
        for index, word in enumerate(result.content.split(" ")):
            yield StreamChunk(delta=word if index == 0 else f" {word}")
        yield StreamChunk(usage=result.usage, finish_reason="stop")


class OpenAIChatAdapter:
    """OpenAI SDK adapter (also covers OpenAI-compatible Llama endpoints)."""

    def generate(
        self,
        *,
        messages: list[ChatMessage],
        model: object,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
    ) -> GenerationResult:
        api_key = settings.OPENAI_API_KEY
        if not api_key:
            raise AIProviderNotConfigured("OPENAI_API_KEY is not configured.")
        from openai import OpenAI

        client_kwargs: dict = {"api_key": api_key}
        if getattr(model, "provider", None) is not None and getattr(model.provider, "base_url", ""):
            client_kwargs["base_url"] = model.provider.base_url
        client = OpenAI(timeout=60, **client_kwargs)

        request: dict = {
            "model": getattr(model, "name", ""),
            "messages": [m.to_openai() for m in messages],
            "temperature": temperature,
        }
        if max_tokens is not None:
            request["max_tokens"] = max_tokens
        if tools:
            request["tools"] = [
                {"type": "function", "function": {**t, "name": t["name"].replace(".", "__")}} for t in tools
            ]

        response = client.chat.completions.create(**request)
        choice = response.choices[0]
        message = choice.message
        content = message.content or ""
        usage = response.usage
        cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        tool_calls = tuple(_parse_openai_tool_calls(getattr(message, "tool_calls", None)))
        return GenerationResult(
            content=content,
            model_name=getattr(model, "name", ""),
            provider_type="openai",
            usage=Usage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                cached_tokens=cached,
            ),
            finish_reason=(getattr(choice, "finish_reason", None) or "stop") or "stop",
            tool_calls=tool_calls,
        )


def _parse_openai_tool_calls(raw):  # noqa: ANN001
    if not raw:
        return []
    out = []
    for tc in raw:
        args_str = getattr(tc.function, "arguments", "") or "{}"
        try:
            args = json.loads(args_str)
        except json.JSONDecodeError, TypeError:
            args = {"_raw": args_str}
        out.append(
            ToolCall(
                id=getattr(tc, "id", "") or "",
                name=(getattr(tc.function, "name", "") or "").replace("__", "."),
                arguments=args,
            )
        )
    return out


# Provider type -> adapter class path. Loaded lazily: provider modules import
# this module for the shared types, so eager imports would be circular.
ADAPTERS = {
    "echo": "apps.ai_gateway.adapters.EchoChatAdapter",
    "openai": "apps.ai_gateway.adapters.OpenAIChatAdapter",
    "google": "apps.ai_gateway.providers.gemini.GeminiChatAdapter",
    "llama": "apps.ai_gateway.providers.llama.LlamaChatAdapter",
}


def get_adapter_for_provider(provider_type: str) -> type[ChatAdapter] | None:
    """Return the adapter class for a provider type (or ``None``)."""
    from django.utils.module_loading import import_string

    path = ADAPTERS.get(provider_type)
    return import_string(path) if path else None


def build_chat_adapter(provider_type: str) -> ChatAdapter:
    """Instantiate the adapter for ``provider_type`` or raise UnsupportedProvider."""
    adapter_cls = get_adapter_for_provider(provider_type)
    if adapter_cls is None:
        raise UnsupportedProvider(f"No chat adapter available for provider type {provider_type!r}.")
    return adapter_cls()
