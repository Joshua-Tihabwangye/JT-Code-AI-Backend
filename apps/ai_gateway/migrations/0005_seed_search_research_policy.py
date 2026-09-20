"""Seed a default routing policy for the SEARCH_RESEARCH task type.

Reuses the same provider/models seeded in 0003_seed_ai_providers. The
LangGraph agent runtime (apps.agents) resolves its model through the gateway,
which requires a default policy for `SEARCH_RESEARCH`.
"""

from django.db import migrations


def seed_search_research_policy(apps, schema_editor):  # noqa: ARG001
    Model = apps.get_model('ai_gateway', 'Model')
    ModelPolicy = apps.get_model('ai_gateway', 'ModelPolicy')
    if ModelPolicy.objects.filter(task_type='SEARCH_RESEARCH').exists():
        return
    primary = Model.objects.filter(name='gemini-2.0-flash').first()
    fallback_a = Model.objects.filter(name='gpt-4o-mini').first()
    fallback_b = Model.objects.filter(name='echo-chat').first()
    if not primary:
        return
    fallbacks = [m for m in (fallback_a, fallback_b) if m]
    policy = ModelPolicy.objects.create(
        name='Search & Research (balanced)',
        slug='search-research',
        description='Default routing policy for the agent runtime (balanced).',
        task_type='SEARCH_RESEARCH',
        routing_strategy='balanced',
        primary_model=primary,
        fallback_policy='any_available',
        is_default=True,
        is_active=True,
    )
    policy.fallback_models.set(fallbacks)


def drop_search_research_policy(apps, schema_editor):  # noqa: ARG001
    ModelPolicy = apps.get_model('ai_gateway', 'ModelPolicy')
    ModelPolicy.objects.filter(task_type='SEARCH_RESEARCH').delete()


class Migration(migrations.Migration):
    dependencies = [
        ('ai_gateway', '0004_alter_provider_type'),
    ]

    operations = [
        migrations.RunPython(seed_search_research_policy, drop_search_research_policy),
    ]
