"""Model routing, generation and usage tracking for the AI gateway.

The gateway is the single policy boundary through which every AI call flows:
it resolves a model (optionally via a ``ModelPolicy``), talks to the provider
through the normalized adapters, records a ``ModelRun`` for every attempt and
settles onto a model in the fallback chain when the primary is unavailable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.ai_gateway.adapters import (
    AIGatewayError,
    AIProviderNotConfigured,
    ChatAdapter,
    ChatMessage,
    GenerationResult,
    ToolCall,
    Usage,
    build_chat_adapter,
)
from apps.ai_gateway.models import Model, ModelPolicy, ModelRun, Provider


class ModelSelectionError(AIGatewayError):
    code = 'MODEL_SELECTION_FAILED'


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
    finish_reason: str = 'stop'


def estimate_cost_usd(model: Model, usage: Usage) -> Decimal:
    """Estimate USD cost for a generation using the model's price table."""
    input_cost = Decimal(usage.input_tokens) * model.input_price_per_token
    output_cost = Decimal(usage.output_tokens) * model.output_price_per_token
    cached_cost = Decimal(usage.cached_tokens) * model.cached_input_price_per_token
    image_cost = Decimal(usage.image_count) * model.image_price_per_unit
    return (input_cost + output_cost + cached_cost + image_cost).quantize(Decimal('0.00000001'))


def select_model(
    *,
    task_type: str,
    model_id: str | None = None,
    policy_slug: str | None = None,
) -> tuple[Model, ModelPolicy | None]:
    """Resolve the model honoring an explicit id, explicit policy or default policy.

    Raises :class:`ModelSelectionError` when nothing usable is resolvable.
    """
    if model_id:
        try:
            model = Model.objects.select_related('provider').get(
                id=model_id, status__in=[Model.Status.ACTIVE, Model.Status.BETA]
            )
        except Model.DoesNotExist as exc:
            raise ModelSelectionError('Model not found', code='MODEL_NOT_FOUND') from exc
        _ensure_usable(model)
        return model, None

    policy: ModelPolicy | None = None
    if policy_slug:
        policy = (
            ModelPolicy.objects.select_related('primary_model', 'primary_model__provider')
            .filter(slug=policy_slug, is_active=True)
            .first()
        )
        if policy is None:
            raise ModelSelectionError('Policy not found', code='MODEL_POLICY_NOT_FOUND')
    else:
        policy = (
            ModelPolicy.objects.select_related('primary_model', 'primary_model__provider')
            .filter(task_type=task_type, is_active=True, is_default=True)
            .first()
        )

    if policy is None:
        raise ModelSelectionError(
            f'No policy found for task type {task_type!r}', code='MODEL_POLICY_NOT_FOUND'
        )
    model = policy.primary_model
    _ensure_usable(model)
    return model, policy


def _ensure_usable(model: Model) -> None:
    if model.provider.status != Provider.Status.ACTIVE:
        raise ModelSelectionError(
            f'Provider {model.provider.name!r} is not active', code='MODEL_PROVIDER_INACTIVE'
        )


def _fallback_models(policy: ModelPolicy | None, primary: Model) -> list[Model]:
    if policy is None:
        return []
    return [
        m
        for m in policy.fallback_models.select_related('provider')
        .filter(status__in=[Model.Status.ACTIVE, Model.Status.BETA])
        .order_by('id')
        if m.id != primary.id
    ]


def generate_completion(
    *,
    messages: list[ChatMessage],
    task_type: str = 'GENERAL_QUESTION',
    model_id: str | None = None,
    policy_slug: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    tools: list[dict] | None = None,
    request_id: str | None = None,
    trace_id: str = '',
    job_id: str | None = None,
    job_step_id: str | None = None,
) -> GenerationOutcome:
    """Generate a completion through the resolved model chain and record a ModelRun.

    Attempts the resolved (primary) model first and then walks the policy fallback
    chain. The ``ModelRun`` row reflects whichever model actually succeeded, with
    token usage, estimated cost, latency and fallback metadata.
    """
    primary, policy = select_model(task_type=task_type, model_id=model_id, policy_slug=policy_slug)
    candidates = [primary, *_fallback_models(policy, primary)]
    request_id = request_id or str(uuid.uuid4())

    run = ModelRun.objects.create(
        request_id=request_id,
        job_id=job_id,
        job_step_id=job_step_id,
        provider=primary.provider,
        model=primary,
        policy=policy,
        status=ModelRun.Status.RUNNING,
        trace_id=trace_id,
    )
    started = timezone.now()
    last_error: AIGatewayError | None = None
    attempted: list[dict] = []

    for index, candidate in enumerate(candidates):
        try:
            adapter: ChatAdapter = build_chat_adapter(candidate.provider.type)
            result: GenerationResult = adapter.generate(
                messages=messages,
                model=candidate,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
            )
        except AIProviderNotConfigured as exc:
            last_error = exc
            attempted.append({'model': candidate.name, 'error_code': exc.code})
            continue
        except AIGatewayError as exc:
            last_error = exc
            attempted.append({'model': candidate.name, 'error_code': exc.code})
            continue
        except Exception as exc:  # noqa: BLE001 - provider SDK/network failures
            wrapped = AIGatewayError(str(exc), code='PROVIDER_CALL_FAILED')
            last_error = wrapped
            attempted.append({'model': candidate.name, 'error_code': wrapped.code})
            continue

        cost = estimate_cost_usd(candidate, result.usage)
        latency = int((timezone.now() - started).total_seconds() * 1000)
        with transaction.atomic():
            run.provider = candidate.provider
            run.model = candidate
            run.status = ModelRun.Status.COMPLETED
            run.input_tokens = result.usage.input_tokens
            run.output_tokens = result.usage.output_tokens
            run.cached_tokens = result.usage.cached_tokens
            run.image_count = result.usage.image_count
            run.provider_cost_usd = cost
            run.estimated_cost_usd = cost
            run.latency_ms = latency
            run.fallback_used = index > 0
            run.retry_count = index
            run.metadata = {'attempted': attempted}
            run.completed_at = timezone.now()
            run.save()
        return GenerationOutcome(
            content=result.content,
            model=candidate,
            provider=candidate.provider,
            policy=policy,
            run=run,
            messages=list(messages),
            usage=result.usage,
            tool_calls=result.tool_calls,
            fallback_used=index > 0,
            finish_reason=result.finish_reason,
        )

    error = last_error or AIGatewayError('All models failed', code='ALL_FALLBACKS_FAILED')
    with transaction.atomic():
        run.status = ModelRun.Status.FAILED
        run.error_code = error.code
        run.error_message = str(error)
        run.metadata = {'attempted': attempted}
        run.completed_at = timezone.now()
        run.save()
    raise error
