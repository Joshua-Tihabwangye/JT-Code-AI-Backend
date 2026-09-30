"""Shared provider plumbing: error taxonomy, HTTP mapping, credentials and tool-name codec.

Every provider failure is normalized into a :class:`ProviderError` subclass that
tells the gateway two things: whether the *same* model may be retried
(``retryable``) and whether the gateway may move on to the next compatible
model (``fallback_allowed``). Provider SDK/HTTP details never leak to callers.
"""

from __future__ import annotations

import email.utils
import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from django.conf import settings

from apps.ai_gateway.adapters import AIGatewayError, AIProviderNotConfigured, ToolCall, Usage


class ProviderError(AIGatewayError):
    """A normalized provider failure."""

    code = "PROVIDER_ERROR"
    retryable = False
    fallback_allowed = True
    # Whether the failure says something about provider health (circuit-breaker input).
    counts_against_circuit = False


class ProviderTimeout(ProviderError):
    code = "PROVIDER_TIMEOUT"
    retryable = True
    counts_against_circuit = True


class ProviderUnavailable(ProviderError):
    code = "PROVIDER_UNAVAILABLE"
    retryable = True
    counts_against_circuit = True


class ProviderRateLimited(ProviderError):
    code = "PROVIDER_RATE_LIMITED"
    retryable = True
    counts_against_circuit = True

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ProviderAuthError(ProviderError):
    """Credentials rejected: a configuration fault, so fall back but never retry."""

    code = "PROVIDER_AUTH_FAILED"
    counts_against_circuit = True


class ProviderBadRequest(ProviderError):
    """The provider rejected this request (e.g. context too long, unsupported option)."""

    code = "PROVIDER_BAD_REQUEST"


class ContentBlocked(ProviderError):
    """Provider safety filters blocked the prompt or output.

    Falling back would be shopping for a less careful model, so it is final.
    """

    code = "CONTENT_BLOCKED"
    fallback_allowed = False


class CircuitOpen(ProviderError):
    code = "PROVIDER_CIRCUIT_OPEN"


class InvalidProviderResponse(ProviderError):
    code = "PROVIDER_INVALID_RESPONSE"
    retryable = True
    counts_against_circuit = True


def parse_retry_after(value: str | None) -> float | None:
    """Parse ``Retry-After`` given as seconds or as an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    return max(0.0, parsed.timestamp() - time.time())


def error_for_response(response: httpx.Response, provider: str) -> ProviderError:
    """Map an unsuccessful HTTP response to the normalized taxonomy (never includes secrets)."""
    status = response.status_code
    message = f"{provider} returned HTTP {status}: {_safe_error_detail(response)}"
    if status == 429:
        return ProviderRateLimited(
            message, retry_after=parse_retry_after(response.headers.get("Retry-After"))
        )
    if status in (408, 504):
        return ProviderTimeout(message)
    if status in (401, 403):
        return ProviderAuthError(message)
    if status >= 500:
        return ProviderUnavailable(message)
    return ProviderBadRequest(message)


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("status") or "")[:300]
    return str(error or body)[:300]


def http_client(timeout: float) -> httpx.Client:
    """Build the outbound HTTP client (tests replace this with a mock transport)."""
    return httpx.Client(timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)), follow_redirects=False)


def post_json(
    url: str, *, headers: dict[str, str], payload: dict[str, Any], timeout: float, provider: str
) -> dict[str, Any]:
    """POST JSON and return the decoded object body, raising normalized errors."""
    try:
        with http_client(timeout) as client:
            response = client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        raise ProviderTimeout(f"{provider} request timed out after {timeout}s.") from exc
    except httpx.TransportError as exc:
        raise ProviderUnavailable(f"{provider} connection failed: {type(exc).__name__}.") from exc
    if response.status_code >= 400:
        raise error_for_response(response, provider)
    try:
        body = response.json()
    except ValueError as exc:
        raise InvalidProviderResponse(f"{provider} returned a non-JSON response.") from exc
    if not isinstance(body, dict):
        raise InvalidProviderResponse(f"{provider} returned an unexpected payload.")
    return body


def stream_sse(
    url: str, *, headers: dict[str, str], payload: dict[str, Any], timeout: float, provider: str
) -> Iterator[dict[str, Any]]:
    """POST and yield decoded ``data:`` JSON objects from a server-sent-event stream."""
    try:
        with (
            http_client(timeout) as client,
            client.stream("POST", url, headers=headers, json=payload) as response,
        ):
            if response.status_code >= 400:
                response.read()
                raise error_for_response(response, provider)
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    decoded = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise InvalidProviderResponse(f"{provider} sent a malformed stream event.") from exc
                if isinstance(decoded, dict):
                    yield decoded
    except httpx.TimeoutException as exc:
        raise ProviderTimeout(f"{provider} stream timed out after {timeout}s.") from exc
    except httpx.TransportError as exc:
        raise ProviderUnavailable(f"{provider} stream failed: {type(exc).__name__}.") from exc


_CREDENTIAL_NAME = re.compile(r"^(GEMINI|LLAMA|OPENAI)_[A-Z0-9_]*API_KEY$")


def resolve_api_key(provider: object, default_name: str) -> str:
    """Return the API key named by ``provider.credentials_ref`` (or ``default_name``).

    Only provider API-key variables are readable: an admin-editable
    ``credentials_ref`` must never be able to exfiltrate other secrets such as
    ``DJANGO_SECRET_KEY`` or database credentials.
    """
    name = (getattr(provider, "credentials_ref", "") or default_name).strip()
    if not _CREDENTIAL_NAME.fullmatch(name):
        raise AIProviderNotConfigured(f"credentials_ref {name!r} is not an allowed provider key name.")
    value = getattr(settings, name, None) or os.environ.get(name, "")
    if not value:
        raise AIProviderNotConfigured(f"{name} is not configured.")
    return str(value)


def provider_model_name(model: object) -> str:
    """Resolve the provider-side model id, optionally overridden from settings.

    ``Model.metadata['provider_model_setting']`` lets a registry row track an
    environment-configured model (``GEMINI_DEFAULT_MODEL``/``LLAMA_DEFAULT_MODEL``)
    so operators can move to a new provider model without a client change.
    """
    metadata = getattr(model, "metadata", None) or {}
    setting_name = str(metadata.get("provider_model_setting", ""))
    if re.fullmatch(r"(GEMINI|LLAMA)_DEFAULT_MODEL", setting_name) and (
        override := getattr(settings, setting_name, "")
    ):
        return str(override)
    return str(getattr(model, "name", ""))


# OpenAI-compatible function names must match ^[A-Za-z0-9_-]{1,64}$ and registry
# tool names use dots (``knowledge.search``), so names are encoded on the wire.
_TOOL_SEPARATOR = "__"


def encode_tool_name(name: str) -> str:
    return name.replace(".", _TOOL_SEPARATOR)


def decode_tool_name(name: str) -> str:
    return name.replace(_TOOL_SEPARATOR, ".")


def normalize_finish_reason(value: str | None) -> str:
    reason = (value or "stop").lower()
    if reason in {"stop", "end_turn", "finish_reason_unspecified"}:
        return "stop"
    if reason in {"length", "max_tokens"}:
        return "length"
    if reason in {"tool_calls", "function_call"}:
        return "tool_calls"
    if reason in {"safety", "content_filter", "recitation", "blocklist", "prohibited_content", "spii"}:
        return "content_filter"
    return reason


@dataclass
class StreamChunk:
    """One normalized streaming event from a provider."""

    delta: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage | None = None
    finish_reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def request_timeout(provider: object) -> float:
    """Per-provider timeout, capped by the gateway-wide ``AI_REQUEST_TIMEOUT_SECONDS``."""
    ceiling = float(settings.AI_REQUEST_TIMEOUT_SECONDS)
    configured = float(getattr(provider, "timeout_seconds", 0) or ceiling)
    return max(1.0, min(configured, ceiling))
