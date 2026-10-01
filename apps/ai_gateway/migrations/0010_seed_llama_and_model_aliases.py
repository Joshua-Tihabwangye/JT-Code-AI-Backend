"""Seed the Llama provider, model aliases and alias-backed task policies (Phase 7).

Clients address aliases; re-pointing an alias's ordered targets swaps providers
without a client change. Gemini and Llama rows track ``GEMINI_DEFAULT_MODEL`` /
``LLAMA_DEFAULT_MODEL`` through ``metadata.provider_model_setting`` so the
provider-side model id is an environment decision. Prices are list prices at
seeding time (USD per token); keep them current in the admin.
"""

from decimal import Decimal

from django.db import migrations

ALIASES = [
    ("default-chat", "General conversation and question answering.", ["chat"]),
    ("tool-calling", "Agent turns that may call registered tools.", ["chat", "tools"]),
    ("fast-chat", "Low-latency, low-cost responses.", ["chat"]),
    ("classification", "Structured intent classification (JSON output).", ["chat", "json_mode"]),
]
POLICY_ALIASES = {
    "GENERAL_QUESTION": "default-chat",
    "RAG_QUERY": "default-chat",
    "SEARCH_RESEARCH": "tool-calling",
}


def seed(apps, schema_editor):  # noqa: ARG001
    Provider = apps.get_model("ai_gateway", "Provider")
    Model = apps.get_model("ai_gateway", "Model")
    ModelAlias = apps.get_model("ai_gateway", "ModelAlias")
    ModelAliasTarget = apps.get_model("ai_gateway", "ModelAliasTarget")
    ModelPolicy = apps.get_model("ai_gateway", "ModelPolicy")

    gemini = Model.objects.filter(provider__type="google", name="gemini-2.0-flash").first()
    if gemini is not None:
        gemini.metadata = {**(gemini.metadata or {}), "provider_model_setting": "GEMINI_DEFAULT_MODEL"}
        gemini.input_price_per_token = Decimal("0.0000003")
        gemini.output_price_per_token = Decimal("0.0000025")
        gemini.cached_input_price_per_token = Decimal("0.000000075")
        gemini.context_window = 1_000_000
        gemini.max_output_tokens = 8192
        gemini.save()

    llama_provider, _ = Provider.objects.get_or_create(
        slug="llama",
        defaults={
            "name": "Llama (OpenAI-compatible)",
            "type": "llama",
            "status": "active",
            "capabilities": ["chat", "function-calling", "streaming"],
            "credentials_ref": "LLAMA_API_KEY",
        },
    )
    llama, _ = Model.objects.get_or_create(
        provider=llama_provider,
        name="llama-default",
        defaults={
            "display_name": "Llama (configured model)",
            "modality": "text",
            "status": "active",
            "context_window": 128_000,
            "max_output_tokens": 4096,
            "supports_tools": True,
            "supports_streaming": True,
            "supports_json_mode": True,
            "input_price_per_token": Decimal("0.00000088"),
            "output_price_per_token": Decimal("0.00000088"),
            "metadata": {"provider_model_setting": "LLAMA_DEFAULT_MODEL"},
        },
    )
    echo = Model.objects.filter(provider__type="echo", name="echo-chat").first()
    chain = [model for model in (gemini, llama, echo) if model is not None]

    aliases = {}
    for slug, description, capabilities in ALIASES:
        alias, created = ModelAlias.objects.get_or_create(
            slug=slug, defaults={"description": description, "required_capabilities": capabilities}
        )
        aliases[slug] = alias
        if created:
            for priority, model in enumerate(chain):
                ModelAliasTarget.objects.create(alias=alias, model=model, priority=priority)

    for task_type, slug in POLICY_ALIASES.items():
        ModelPolicy.objects.filter(task_type=task_type, is_default=True, model_alias__isnull=True).update(
            model_alias=aliases[slug]
        )


def unseed(apps, schema_editor):  # noqa: ARG001
    ModelPolicy = apps.get_model("ai_gateway", "ModelPolicy")
    ModelAlias = apps.get_model("ai_gateway", "ModelAlias")
    Model = apps.get_model("ai_gateway", "Model")
    Provider = apps.get_model("ai_gateway", "Provider")
    slugs = [slug for slug, _description, _capabilities in ALIASES]
    ModelPolicy.objects.filter(model_alias__slug__in=slugs).update(model_alias=None)
    ModelAlias.objects.filter(slug__in=slugs).delete()
    Model.objects.filter(provider__slug="llama", name="llama-default").delete()
    Provider.objects.filter(slug="llama").delete()


class Migration(migrations.Migration):
    dependencies = [("ai_gateway", "0009_model_aliases_and_run_attribution")]

    operations = [migrations.RunPython(seed, unseed)]
