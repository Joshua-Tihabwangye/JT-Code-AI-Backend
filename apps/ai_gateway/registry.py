"""Model aliases and the capability registry.

Clients address models by *alias* (``default-chat``, ``tool-calling`` …), never
by provider model name. An alias maps to an ordered list of target models;
the gateway tries them in order, skipping any model that is unusable or lacks
a capability the request needs. Re-pointing an alias therefore swaps providers
without any client API change.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from django.conf import settings

from apps.ai_gateway.adapters import AIGatewayError
from apps.ai_gateway.models import Model, ModelAlias, ModelPolicy, Provider

CHAT = "chat"
TOOLS = "tools"
STREAMING = "streaming"
JSON_MODE = "json_mode"
VISION = "vision"
EMBEDDING = "embedding"
KNOWN_CAPABILITIES = frozenset({CHAT, TOOLS, STREAMING, JSON_MODE, VISION, EMBEDDING})
DEFAULT_TASK_TYPE = "GENERAL_QUESTION"


class ModelSelectionError(AIGatewayError):
    code = "MODEL_SELECTION_FAILED"


def model_capabilities(model: Model) -> frozenset[str]:
    """Capabilities derived from the registry row (the single source of truth)."""
    capabilities: set[str] = set()
    if model.modality in {Model.Modality.TEXT, Model.Modality.MULTIMODAL}:
        capabilities.add(CHAT)
    if model.modality == Model.Modality.EMBEDDING:
        capabilities.add(EMBEDDING)
    if model.supports_tools:
        capabilities.add(TOOLS)
    if model.supports_streaming:
        capabilities.add(STREAMING)
    if model.supports_json_mode:
        capabilities.add(JSON_MODE)
    if model.supports_vision or model.modality == Model.Modality.MULTIMODAL:
        capabilities.add(VISION)
    return frozenset(capabilities)


def is_usable(model: Model) -> bool:
    return (
        model.status in {Model.Status.ACTIVE, Model.Status.BETA}
        and model.provider.status == Provider.Status.ACTIVE
    )


@dataclass
class Resolution:
    """The ordered, capability-compatible candidates for one request."""

    candidates: list[Model]
    policy: ModelPolicy | None = None
    alias: str = ""
    required_capabilities: frozenset[str] = frozenset()
    skipped: list[dict[str, str]] = field(default_factory=list)


def _alias(slug: str) -> ModelAlias:
    alias = ModelAlias.objects.filter(slug=slug, is_active=True).first()
    if alias is None:
        raise ModelSelectionError(f"Model alias {slug!r} does not exist.", code="MODEL_ALIAS_NOT_FOUND")
    return alias


def _alias_models(alias: ModelAlias) -> list[Model]:
    return [
        target.model
        for target in alias.targets.select_related("model", "model__provider").order_by("priority", "id")
    ]


def _policy_models(policy: ModelPolicy) -> tuple[list[Model], str, frozenset[str]]:
    alias = policy.model_alias
    if alias is not None:
        return _alias_models(alias), alias.slug, frozenset(alias.required_capabilities or ())
    fallbacks = list(policy.fallback_models.select_related("provider").order_by("id"))
    ordered = [policy.primary_model, *[m for m in fallbacks if m.id != policy.primary_model_id]]
    return ordered, "", frozenset(policy.required_capabilities or ())


def resolve_candidates(
    *,
    task_type: str = DEFAULT_TASK_TYPE,
    model_id: str | None = None,
    policy_slug: str | None = None,
    model_alias: str | None = None,
    required_capabilities: Iterable[str] = (),
) -> Resolution:
    """Resolve an explicit model, alias, policy or task-type default into candidates."""
    requested = frozenset(required_capabilities)
    unknown = requested - KNOWN_CAPABILITIES
    if unknown:
        raise ModelSelectionError(f"Unknown capabilities: {sorted(unknown)}", code="INVALID_INPUT")

    policy: ModelPolicy | None = None
    alias_slug = ""
    declared: frozenset[str] = frozenset()
    if model_id:
        model = Model.objects.select_related("provider").filter(id=model_id).first()
        if model is None:
            raise ModelSelectionError("Model not found", code="MODEL_NOT_FOUND")
        ordered = [model]
    elif model_alias:
        alias = _alias(model_alias)
        ordered, alias_slug = _alias_models(alias), alias.slug
        declared = frozenset(alias.required_capabilities or ())
    else:
        policies = ModelPolicy.objects.select_related(
            "primary_model", "primary_model__provider", "model_alias"
        ).filter(is_active=True)
        if policy_slug:
            policy = policies.filter(slug=policy_slug).first()
            if policy is None:
                raise ModelSelectionError("Policy not found", code="MODEL_POLICY_NOT_FOUND")
        else:
            policy = policies.filter(task_type=task_type, is_default=True).first()
        if policy is not None:
            ordered, alias_slug, declared = _policy_models(policy)
        elif task_type == DEFAULT_TASK_TYPE:
            # General chat always has a route: the configured default alias.
            alias = _alias(settings.AI_DEFAULT_MODEL_ALIAS)
            ordered, alias_slug = _alias_models(alias), alias.slug
            declared = frozenset(alias.required_capabilities or ())
        else:
            raise ModelSelectionError(
                f"No policy found for task type {task_type!r}", code="MODEL_POLICY_NOT_FOUND"
            )

    needed = requested | declared | {CHAT}
    resolution = Resolution(candidates=[], policy=policy, alias=alias_slug, required_capabilities=needed)
    for model in ordered:
        if not is_usable(model):
            resolution.skipped.append({"model": model.name, "reason": "unavailable"})
            continue
        missing = needed - model_capabilities(model)
        if missing:
            resolution.skipped.append({"model": model.name, "reason": f"missing:{','.join(sorted(missing))}"})
            continue
        resolution.candidates.append(model)
    if not resolution.candidates:
        raise ModelSelectionError(f"No available model provides {sorted(needed)}.", code="NO_CAPABLE_MODEL")
    if not settings.AI_GATEWAY_FALLBACK_ENABLED:
        resolution.candidates = resolution.candidates[:1]
    return resolution


def system_capabilities() -> dict[str, object]:
    """Public, secret-free description of what the AI gateway can do right now."""
    aliases = []
    for alias in ModelAlias.objects.filter(is_active=True).order_by("slug"):
        usable = [m for m in _alias_models(alias) if is_usable(m)]
        capabilities: set[str] = set()
        for model in usable:
            capabilities |= model_capabilities(model)
        aliases.append(
            {
                "alias": alias.slug,
                "description": alias.description,
                "available": bool(usable),
                "capabilities": sorted(capabilities),
                "isDefault": alias.slug == settings.AI_DEFAULT_MODEL_ALIAS,
            }
        )
    return {
        "defaultAlias": settings.AI_DEFAULT_MODEL_ALIAS,
        "aliases": aliases,
        "providers": {
            "gemini": bool(settings.GEMINI_API_KEY),
            "llama": bool(settings.LLAMA_API_KEY and settings.LLAMA_API_BASE),
        },
        "fallbackEnabled": settings.AI_GATEWAY_FALLBACK_ENABLED,
    }
