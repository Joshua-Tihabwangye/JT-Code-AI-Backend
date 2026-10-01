"""The AI gateway: routing, resilience, metering and normalized results.

Every model call in JT-Code flows through :func:`generate_completion` (or
:func:`stream_completion`). The gateway

1. resolves an alias / policy / task type into capability-compatible candidates
   (:mod:`apps.ai_gateway.registry`),
2. skips candidates whose estimated cost exceeds the policy/global ceiling,
3. calls each candidate behind its provider circuit breaker with bounded,
   jittered retries inside one request deadline (:mod:`apps.ai_gateway.resilience`),
4. falls back to the next compatible model only when the error allows it, and
5. records one ``ModelRun`` with alias, per-attempt outcomes, retries, latency,
   tokens and estimated cost.

Callers never see provider SDKs, provider model names or provider errors.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.ai_gateway.adapters import (
    AIGatewayError,
    AIProviderNotConfigured,
    ChatAdapter,
    ChatMessage,
    GenerationResult,
    ToolCall,
    UnsupportedProvider,
    Usage,
    build_chat_adapter,
    estimate_tokens,
)
from apps.ai_gateway.models import Model, ModelPolicy, ModelRun, Provider
from apps.ai_gateway.providers.base import (
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from apps.ai_gateway.registry import STREAMING, TOOLS, ModelSelectionError, Resolution, resolve_candidates
from apps.ai_gateway.resilience import CircuitBreaker, Deadline, call_with_retry

__all__ = [
    "BudgetExceeded",
    "GenerationOutcome",
    "ModelSelectionError",
    "estimate_cost_usd",
    "estimate_request_cost",
    "generate_completion",
    "select_model",
    "stream_completion",
]


class BudgetExceeded(AIGatewayError):
    code = "AI_BUDGET_EXCEEDED"


@dataclass
class GenerationOutcome:
    """Result of a successful generation plus the execution record."""

    content: str
    model: Model
    provider: Provider
    policy: ModelPolicy | None
    run: ModelRun
    messages: list[ChatMessage]
    usage: Usage
    tool_calls: tuple[ToolCall, ...] = ()
    fallback_used: bool = False
    finish_reason: str = "stop"
    model_alias: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)


def estimate_cost_usd(model: Model, usage: Usage) -> Decimal:
    """Estimate USD cost for a generation using the model's price table."""
    input_cost = Decimal(usage.input_tokens) * model.input_price_per_token
    output_cost = Decimal(usage.output_tokens) * model.output_price_per_token
    cached_cost = Decimal(usage.cached_tokens) * model.cached_input_price_per_token
    image_cost = Decimal(usage.image_count) * model.image_price_per_unit
    return (input_cost + output_cost + cached_cost + image_cost).quantize(Decimal("0.00000001"))


def estimate_request_cost(model: Model, messages: list[ChatMessage], max_tokens: int | None) -> Decimal:
    """Upper-bound cost before calling: full prompt plus the maximum output budget."""
    prompt_tokens = estimate_tokens(" ".join(message.content for message in messages))
    output_tokens = min(max_tokens or model.max_output_tokens, model.max_output_tokens or 4096)
    return estimate_cost_usd(model, Usage(input_tokens=prompt_tokens, output_tokens=output_tokens))


def _cost_ceiling(policy: ModelPolicy | None) -> Decimal | None:
    ceilings = []
    if settings.AI_GATEWAY_MAX_COST_USD and settings.AI_GATEWAY_MAX_COST_USD > 0:
        ceilings.append(Decimal(str(settings.AI_GATEWAY_MAX_COST_USD)))
    if policy is not None and policy.max_cost_usd is not None:
        ceilings.append(policy.max_cost_usd)
    return min(ceilings) if ceilings else None


def _deadline(policy: ModelPolicy | None) -> Deadline:
    budget = settings.AI_GATEWAY_MAX_LATENCY_MS
    if policy is not None and policy.max_latency_ms:
        budget = min(budget, policy.max_latency_ms)
    return Deadline(budget)


def normalize_exception(exc: Exception) -> AIGatewayError:
    """Map SDK/transport exceptions that escaped an adapter onto the gateway taxonomy."""
    if isinstance(exc, AIGatewayError):
        return exc
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__.lower()
    if status == 429 or "ratelimit" in name:
        return ProviderRateLimited(str(exc)[:300])
    if "timeout" in name:
        return ProviderTimeout(str(exc)[:300])
    if (isinstance(status, int) and status >= 500) or "connection" in name:
        return ProviderUnavailable(str(exc)[:300])
    return AIGatewayError(str(exc)[:300], code="PROVIDER_CALL_FAILED")


def select_model(
    *,
    task_type: str,
    model_id: str | None = None,
    policy_slug: str | None = None,
    model_alias: str | None = None,
) -> tuple[Model, ModelPolicy | None]:
    """Return the first usable candidate (used for request validation and display)."""
    resolution = resolve_candidates(
        task_type=task_type, model_id=model_id, policy_slug=policy_slug, model_alias=model_alias
    )
    return resolution.candidates[0], resolution.policy


def _resolve(
    *,
    task_type: str,
    model_id: str | None,
    policy_slug: str | None,
    model_alias: str | None,
    tools: list[dict] | None,
    streaming: bool = False,
) -> Resolution:
    required = set()
    if tools:
        required.add(TOOLS)
    if streaming:
        required.add(STREAMING)
    return resolve_candidates(
        task_type=task_type,
        model_id=model_id,
        policy_slug=policy_slug,
        model_alias=model_alias,
        required_capabilities=required,
    )


class _Attempts:
    """Collects per-candidate outcomes for the ``ModelRun`` audit trail."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.retries = 0
        self.last_error: AIGatewayError | None = None
        self.budget_skips = 0

    def start(self, model: Model) -> dict[str, Any]:
        entry: dict[str, Any] = {"provider": model.provider.slug, "model": model.name, "retries": 0}
        self.items.append(entry)
        return entry

    def retried(self, entry: dict[str, Any], error: ProviderError) -> None:
        entry["retries"] += 1
        entry.setdefault("retry_errors", []).append(error.code)
        self.retries += 1

    def failed(self, entry: dict[str, Any], error: AIGatewayError, started: float) -> None:
        entry["error_code"] = error.code
        entry["latency_ms"] = int((time.monotonic() - started) * 1000)
        self.last_error = error


def _admit(
    candidate: Model,
    *,
    attempts: _Attempts,
    ceiling: Decimal | None,
    messages: list[ChatMessage],
    max_tokens: int | None,
) -> tuple[dict[str, Any], CircuitBreaker] | None:
    """Apply the cost ceiling and circuit breaker; ``None`` means skip this candidate."""
    entry = attempts.start(candidate)
    if ceiling is not None and estimate_request_cost(candidate, messages, max_tokens) > ceiling:
        entry["error_code"] = BudgetExceeded.code
        attempts.budget_skips += 1
        attempts.last_error = BudgetExceeded("Estimated cost exceeds the configured ceiling.")
        return None
    breaker = CircuitBreaker.for_provider(candidate.provider)
    try:
        breaker.before_call()
    except ProviderError as exc:
        entry["error_code"] = exc.code
        attempts.last_error = exc
        return None
    return entry, breaker


def _final_error(attempts: _Attempts, candidates: list[Model]) -> AIGatewayError:
    if attempts.budget_skips and attempts.budget_skips == len(attempts.items) == len(candidates):
        return BudgetExceeded("Every candidate model exceeds the configured cost ceiling.")
    return attempts.last_error or AIGatewayError("All models failed", code="ALL_FALLBACKS_FAILED")


def _finish_run(
    run: ModelRun,
    *,
    model: Model,
    usage: Usage,
    started: float,
    attempts: _Attempts,
    fallback_used: bool,
) -> None:
    cost = estimate_cost_usd(model, usage)
    run.provider = model.provider
    run.model = model
    run.status = ModelRun.Status.COMPLETED
    run.input_tokens = usage.input_tokens
    run.output_tokens = usage.output_tokens
    run.cached_tokens = usage.cached_tokens
    run.image_count = usage.image_count
    run.provider_cost_usd = cost
    run.estimated_cost_usd = cost
    run.latency_ms = int((time.monotonic() - started) * 1000)
    run.fallback_used = fallback_used
    run.retry_count = attempts.retries
    run.metadata = {**(run.metadata or {}), "attempts": attempts.items}
    run.completed_at = timezone.now()
    run.save()


def _fail_run(run: ModelRun, *, error: AIGatewayError, started: float, attempts: _Attempts) -> None:
    run.status = ModelRun.Status.TIMEOUT if isinstance(error, ProviderTimeout) else ModelRun.Status.FAILED
    run.error_code = error.code
    run.error_message = str(error)[:2000]
    run.latency_ms = int((time.monotonic() - started) * 1000)
    run.retry_count = attempts.retries
    run.metadata = {**(run.metadata or {}), "attempts": attempts.items}
    run.completed_at = timezone.now()
    run.save()


def _open_run(
    resolution: Resolution,
    *,
    request_id: str | None,
    trace_id: str,
    job_id: str | None,
    job_step_id: str | None,
    organization_id,
) -> ModelRun:
    primary = resolution.candidates[0]
    return ModelRun.objects.create(
        request_id=request_id or str(uuid.uuid4()),
        job_id=job_id,
        job_step_id=job_step_id,
        organization_id=organization_id,
        provider=primary.provider,
        model=primary,
        policy=resolution.policy,
        model_alias=resolution.alias,
        status=ModelRun.Status.RUNNING,
        trace_id=trace_id,
        metadata={"skipped": resolution.skipped} if resolution.skipped else {},
    )


def generate_completion(
    *,
    messages: list[ChatMessage],
    task_type: str = "GENERAL_QUESTION",
    model_id: str | None = None,
    policy_slug: str | None = None,
    model_alias: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    tools: list[dict] | None = None,
    request_id: str | None = None,
    trace_id: str = "",
    job_id: str | None = None,
    job_step_id: str | None = None,
    organization_id=None,
) -> GenerationOutcome:
    """Generate a completion through the resolved, compatible model chain."""
    resolution = _resolve(
        task_type=task_type, model_id=model_id, policy_slug=policy_slug, model_alias=model_alias, tools=tools
    )
    run = _open_run(
        resolution,
        request_id=request_id,
        trace_id=trace_id,
        job_id=job_id,
        job_step_id=job_step_id,
        organization_id=organization_id,
    )
    started = time.monotonic()
    deadline = _deadline(resolution.policy)
    ceiling = _cost_ceiling(resolution.policy)
    attempts = _Attempts()

    for index, candidate in enumerate(resolution.candidates):
        if deadline.remaining() <= 0:
            attempts.last_error = ProviderTimeout("The AI gateway request deadline was exceeded.")
            break
        admitted = _admit(
            candidate, attempts=attempts, ceiling=ceiling, messages=messages, max_tokens=max_tokens
        )
        if admitted is None:
            continue
        entry, breaker = admitted
        attempt_started = time.monotonic()
        try:
            adapter: ChatAdapter = build_chat_adapter(candidate.provider.type)

            def call(adapter: ChatAdapter = adapter, candidate: Model = candidate) -> GenerationResult:
                try:
                    return adapter.generate(
                        messages=messages,
                        model=candidate,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        tools=tools,
                    )
                except AIGatewayError:
                    raise
                except Exception as exc:  # noqa: BLE001 - normalize SDK/transport failures
                    raise normalize_exception(exc) from exc

            result = call_with_retry(
                call,
                max_retries=min(candidate.provider.max_retries, settings.AI_MAX_RETRIES),
                deadline=deadline,
                on_retry=lambda _n, error, entry=entry: attempts.retried(entry, error),
            )
        except (AIProviderNotConfigured, UnsupportedProvider) as exc:
            attempts.failed(entry, exc, attempt_started)
            continue
        except ProviderError as exc:
            breaker.record_failure(exc)
            attempts.failed(entry, exc, attempt_started)
            if not exc.fallback_allowed:
                break
            continue
        except AIGatewayError as exc:
            attempts.failed(entry, exc, attempt_started)
            continue

        breaker.record_success()
        entry["latency_ms"] = int((time.monotonic() - attempt_started) * 1000)
        fallback_used = index > 0
        _finish_run(
            run,
            model=candidate,
            usage=result.usage,
            started=started,
            attempts=attempts,
            fallback_used=fallback_used,
        )
        return GenerationOutcome(
            content=result.content,
            model=candidate,
            provider=candidate.provider,
            policy=resolution.policy,
            run=run,
            messages=list(messages),
            usage=result.usage,
            tool_calls=result.tool_calls,
            fallback_used=fallback_used,
            finish_reason=result.finish_reason,
            model_alias=resolution.alias,
            attempts=attempts.items,
        )

    error = _final_error(attempts, resolution.candidates)
    _fail_run(run, error=error, started=started, attempts=attempts)
    raise error


def _chunks(adapter: Any, **kwargs: Any) -> Iterator[Any]:
    """Stream from the adapter, or emit one chunk from ``generate`` if it cannot stream."""
    from apps.ai_gateway.providers.base import StreamChunk

    if hasattr(adapter, "stream"):
        yield from adapter.stream(**kwargs)
        return
    result = adapter.generate(**kwargs)
    yield StreamChunk(delta=result.content, tool_calls=result.tool_calls, finish_reason=result.finish_reason)
    yield StreamChunk(usage=result.usage)


def stream_completion(
    *,
    messages: list[ChatMessage],
    task_type: str = "GENERAL_QUESTION",
    model_alias: str | None = None,
    policy_slug: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    tools: list[dict] | None = None,
    request_id: str | None = None,
    trace_id: str = "",
    organization_id=None,
) -> Iterator[dict[str, Any]]:
    """Yield normalized ``delta`` / ``tool_calls`` / ``done`` events.

    Fallback happens only before the first chunk reaches the caller; once
    output has been emitted a failure is final (it cannot be un-sent).
    """
    resolution = _resolve(
        task_type=task_type,
        model_id=None,
        policy_slug=policy_slug,
        model_alias=model_alias,
        tools=tools,
        streaming=True,
    )
    run = _open_run(
        resolution,
        request_id=request_id,
        trace_id=trace_id,
        job_id=None,
        job_step_id=None,
        organization_id=organization_id,
    )
    started = time.monotonic()
    deadline = _deadline(resolution.policy)
    ceiling = _cost_ceiling(resolution.policy)
    attempts = _Attempts()

    for index, candidate in enumerate(resolution.candidates):
        if deadline.remaining() <= 0:
            attempts.last_error = ProviderTimeout("The AI gateway request deadline was exceeded.")
            break
        admitted = _admit(
            candidate, attempts=attempts, ceiling=ceiling, messages=messages, max_tokens=max_tokens
        )
        if admitted is None:
            continue
        entry, breaker = admitted
        attempt_started = time.monotonic()
        try:
            adapter = build_chat_adapter(candidate.provider.type)
            iterator = _chunks(
                adapter,
                messages=messages,
                model=candidate,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
            )
            first = next(iterator)
        except StopIteration:
            error = AIGatewayError("Provider returned an empty stream.", code="PROVIDER_EMPTY_STREAM")
            attempts.failed(entry, error, attempt_started)
            continue
        except (AIProviderNotConfigured, UnsupportedProvider) as exc:
            attempts.failed(entry, exc, attempt_started)
            continue
        except Exception as exc:  # noqa: BLE001 - normalized below
            normalized = normalize_exception(exc)
            if isinstance(normalized, ProviderError):
                breaker.record_failure(normalized)
            attempts.failed(entry, normalized, attempt_started)
            if isinstance(normalized, ProviderError) and not normalized.fallback_allowed:
                break
            continue

        usage = Usage()
        finish = "stop"
        try:
            for chunk in _chain(first, iterator):
                if chunk.delta:
                    yield {"type": "delta", "text": chunk.delta}
                if chunk.tool_calls:
                    yield {
                        "type": "tool_calls",
                        "toolCalls": [
                            {"id": call.id, "name": call.name, "arguments": call.arguments}
                            for call in chunk.tool_calls
                        ],
                    }
                if chunk.usage is not None:
                    usage = chunk.usage
                if chunk.finish_reason:
                    finish = chunk.finish_reason
        except Exception as exc:  # noqa: BLE001 - mid-stream failure is final
            normalized = normalize_exception(exc)
            if isinstance(normalized, ProviderError):
                breaker.record_failure(normalized)
            attempts.failed(entry, normalized, attempt_started)
            _fail_run(run, error=normalized, started=started, attempts=attempts)
            raise normalized from exc

        breaker.record_success()
        entry["latency_ms"] = int((time.monotonic() - attempt_started) * 1000)
        _finish_run(
            run, model=candidate, usage=usage, started=started, attempts=attempts, fallback_used=index > 0
        )
        yield {
            "type": "done",
            "modelAlias": resolution.alias,
            "modelRunId": str(run.id),
            "finishReason": finish,
            "usage": {"inputTokens": usage.input_tokens, "outputTokens": usage.output_tokens},
        }
        return

    error = _final_error(attempts, resolution.candidates)
    _fail_run(run, error=error, started=started, attempts=attempts)
    raise error


def _chain(first: Any, rest: Iterator[Any]) -> Iterator[Any]:
    yield first
    yield from rest
