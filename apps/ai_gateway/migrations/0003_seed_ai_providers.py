"""Seed default AI providers, models and routing policies for task types.

Default routing uses a real provider (Gemini) first, then falls back to
gpt-4o-mini and finally to the local ``echo`` model. The ``echo`` adapter only
responds when ``AI_PROVIDER=echo`` (dev/test), so it never answers in a
production deployment that does not opt in explicitly.
"""

import uuid

from django.db import migrations

PROVIDERS = [
    {
        'name': 'Google (Gemini)',
        'slug': 'google-gemini',
        'type': 'google',
        'base_url': '',
        'capabilities': ['chat', 'function-calling', 'streaming'],
    },
    {
        'name': 'OpenAI',
        'slug': 'openai',
        'type': 'openai',
        'base_url': '',
        'capabilities': ['chat', 'function-calling', 'streaming'],
    },
    {
        'name': 'Local Echo (Dev)',
        'slug': 'echo',
        'type': 'echo',
        'base_url': '',
        'capabilities': ['chat'],
    },
]

MODELS = [
    ('google-gemini', 'gemini-2.0-flash', 'Gemini 2.0 Flash'),
    ('openai', 'gpt-4o-mini', 'GPT-4o mini'),
    ('echo', 'echo-chat', 'Echo (Dev) chat'),
]

POLICIES = [
    {
        'name': 'General Question (balanced)',
        'slug': 'general-question',
        'task_type': 'GENERAL_QUESTION',
        'primary_model': 'gemini-2.0-flash',
        'fallback_models': ['gpt-4o-mini', 'echo-chat'],
    },
    {
        'name': 'RAG Query (balanced)',
        'slug': 'rag-query',
        'task_type': 'RAG_QUERY',
        'primary_model': 'gemini-2.0-flash',
        'fallback_models': ['gpt-4o-mini', 'echo-chat'],
    },
]


def seed_ai_registry(apps, schema_editor):  # noqa: ARG001
    Provider = apps.get_model('ai_gateway', 'Provider')
    Model = apps.get_model('ai_gateway', 'Model')
    ModelPolicy = apps.get_model('ai_gateway', 'ModelPolicy')
    if ModelPolicy.objects.exists():
        return

    providers = {}
    for data in PROVIDERS:
        providers[data['slug']] = Provider.objects.create(
            id=uuid.uuid4(),
            name=data['name'],
            slug=data['slug'],
            type=data['type'],
            base_url=data['base_url'],
            capabilities=data['capabilities'],
            status='active',
        )

    models = {}
    for provider_slug, name, display_name in MODELS:
        models[name] = Model.objects.create(
            id=uuid.uuid4(),
            provider=providers[provider_slug],
            name=name,
            display_name=display_name,
            modality='text',
            status='active',
            context_window=200000,
            max_output_tokens=4096,
            supports_tools=True,
            supports_streaming=True,
            supports_json_mode=True,
        )

    for data in POLICIES:
        policy = ModelPolicy.objects.create(
            id=uuid.uuid4(),
            name=data['name'],
            slug=data['slug'],
            description='Default routing policy (balanced).',
            task_type=data['task_type'],
            routing_strategy='balanced',
            primary_model=models[data['primary_model']],
            fallback_policy='any_available',
            is_default=True,
            is_active=True,
        )
        policy.fallback_models.set([models[name] for name in data['fallback_models']])


def remove_ai_registry(apps, schema_editor):  # noqa: ARG001
    ModelPolicy = apps.get_model('ai_gateway', 'ModelPolicy')
    Model = apps.get_model('ai_gateway', 'Model')
    Provider = apps.get_model('ai_gateway', 'Provider')
    ModelPolicy.objects.filter(slug__in=[p['slug'] for p in POLICIES]).delete()
    Model.objects.filter(name__in=[name for _, name, _ in MODELS]).delete()
    Provider.objects.filter(slug__in=[p['slug'] for p in PROVIDERS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('ai_gateway', '0002_remove_model_ai_gateway_model_provider_name_uniq_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_ai_registry, remove_ai_registry),
    ]
