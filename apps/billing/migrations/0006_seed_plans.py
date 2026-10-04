"""Seed the plan catalogue the frontend expects (free/pro/team/business).

Prices, credits, quotas and limits are editable defaults: rows that already
exist are never overwritten. Link them to Stripe with
``manage.py sync_stripe_prices``.
"""

from decimal import Decimal

from django.db import migrations

FREE_QUOTAS = {
    "chat_messages": 300,
    "rag_queries": 100,
    "search_queries": 20,
    "image_generations": 10,
    "agent_runs": 20,
    "analysis_runs": 10,
    "document_renders": 20,
    "file_conversions": 20,
    "knowledge_documents": 50,
}
PLANS = [
    {
        "slug": "free",
        "name": "Free",
        "description": "Try JT-Code with monthly starter credits.",
        "price_cents": 0,
        "price_yearly_cents": 0,
        "monthly_credits": Decimal("100"),
        "sort_order": 0,
        "is_popular": False,
        "features": ["100 credits every month", "Chat, knowledge search and documents", "Community support"],
        "limits": {
            "max_concurrent_jobs": 2,
            "max_concurrent_chat_requests": 3,
            "max_concurrent_agent_runs": 1,
            "max_concurrent_analysis_runs": 1,
            "rate_multiplier": 2,
        },
        "quotas": {feature: ("hard", limit) for feature, limit in FREE_QUOTAS.items()},
    },
    {
        "slug": "pro",
        "name": "Pro",
        "description": "For individuals who use JT-Code every day.",
        "price_cents": 2000,
        "price_yearly_cents": 20000,
        "monthly_credits": Decimal("1500"),
        "sort_order": 1,
        "is_popular": True,
        "features": ["1,500 credits every month", "Agents and deep research", "Email support"],
        "limits": {
            "max_concurrent_jobs": 5,
            "max_concurrent_chat_requests": 10,
            "max_concurrent_agent_runs": 3,
            "max_concurrent_analysis_runs": 2,
            "rate_multiplier": 5,
        },
        "quotas": {"image_generations": ("hard", 500)},
    },
    {
        "slug": "team",
        "name": "Team",
        "description": "Shared knowledge and higher limits for teams.",
        "price_cents": 6000,
        "price_yearly_cents": 60000,
        "monthly_credits": Decimal("5000"),
        "sort_order": 2,
        "is_popular": False,
        "features": ["5,000 credits every month", "Shared knowledge bases", "Priority support"],
        "limits": {
            "max_concurrent_jobs": 20,
            "max_concurrent_chat_requests": 30,
            "max_concurrent_agent_runs": 10,
            "max_concurrent_analysis_runs": 5,
            "rate_multiplier": 10,
        },
        "quotas": {},
    },
    {
        "slug": "business",
        "name": "Business",
        "description": "High-volume usage with the highest limits.",
        "price_cents": 20000,
        "price_yearly_cents": 200000,
        "monthly_credits": Decimal("18000"),
        "sort_order": 3,
        "is_popular": False,
        "features": ["18,000 credits every month", "Highest concurrency and rate limits", "Dedicated support"],
        "limits": {
            "max_concurrent_jobs": 50,
            "max_concurrent_chat_requests": 60,
            "max_concurrent_agent_runs": 25,
            "max_concurrent_analysis_runs": 10,
            "rate_multiplier": 20,
        },
        "quotas": {},
    },
]


def seed(apps, schema_editor):  # noqa: ARG001
    Plan = apps.get_model("billing", "Plan")
    Entitlement = apps.get_model("billing", "Entitlement")
    for definition in PLANS:
        values = dict(definition)
        quotas = values.pop("quotas")
        plan, created = Plan.objects.get_or_create(
            slug=values.pop("slug"), defaults={**values, "interval": "monthly", "currency": "USD"}
        )
        if not created:
            continue
        for feature, (limit_type, limit) in quotas.items():
            Entitlement.objects.create(
                plan=plan, feature=feature, limit_type=limit_type, limit_value=limit, reset_period="monthly"
            )


class Migration(migrations.Migration):
    dependencies = [("billing", "0005_stripe_billing")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
