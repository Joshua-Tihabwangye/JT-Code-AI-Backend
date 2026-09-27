"""Create portable, read-only analytics views.

PostgreSQL requires ``CREATE OR REPLACE VIEW`` while SQLite accepts neither
``OR REPLACE`` nor PostgreSQL role grants.  Running the DDL through a
vendor-aware migration keeps local tests useful while exercising the production
syntax in CI's PostgreSQL service.
"""

from django.db import migrations

VIEWS = {
    "analytics_job_summary": """
        SELECT
            organization_id,
            task_type,
            status,
            COUNT(*) AS total_jobs,
            SUM(reserved_credits) AS reserved_credits,
            SUM(COALESCE(actual_credits, 0)) AS actual_credits
        FROM jobs_job
        GROUP BY organization_id, task_type, status
    """,
    "analytics_usage_ledger": """
        SELECT
            wallet.organization_id AS organization_id,
            ledger.direction,
            ledger.reason,
            COUNT(*) AS entry_count,
            SUM(ledger.credits) AS total_credits
        FROM billing_creditledger ledger
        JOIN billing_creditwallet wallet ON wallet.id = ledger.wallet_id
        GROUP BY wallet.organization_id, ledger.direction, ledger.reason
    """,
    "analytics_billing_summary": """
        SELECT
            wallet.organization_id AS organization_id,
            wallet.balance,
            wallet.reserved_balance,
            subscription.status AS subscription_status,
            subscription.plan_id
        FROM billing_creditwallet wallet
        LEFT JOIN billing_subscription subscription
            ON subscription.organization_id = wallet.organization_id
            AND subscription.status IN ('active', 'trialing')
    """,
    "analytics_conversation_summary": """
        SELECT
            conversation.organization_id AS organization_id,
            COUNT(DISTINCT conversation.id) AS conversation_count,
            COUNT(message.id) AS message_count
        FROM conversations_conversation conversation
        LEFT JOIN conversations_message message
            ON message.conversation_id = conversation.id
        GROUP BY conversation.organization_id
    """,
    "analytics_asset_summary": """
        SELECT
            organization_id,
            resource_type,
            status,
            COUNT(*) AS asset_count,
            SUM(bytes) AS total_bytes
        FROM assets_asset
        GROUP BY organization_id, resource_type, status
    """,
}


def create_analytics_views(apps, schema_editor):  # noqa: ARG001
    create = (
        "CREATE OR REPLACE VIEW"
        if schema_editor.connection.vendor == "postgresql"
        else "CREATE VIEW IF NOT EXISTS"
    )
    with schema_editor.connection.cursor() as cursor:
        for name, query in VIEWS.items():
            cursor.execute(f"{create} {name} AS {query}")  # nosec B608 -- constant migration DDL.


def drop_analytics_views(apps, schema_editor):  # noqa: ARG001
    with schema_editor.connection.cursor() as cursor:
        for name in VIEWS:
            cursor.execute(f"DROP VIEW IF EXISTS {name}")  # nosec B608 -- constant migration DDL.


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0003_asset_organization"),
        ("billing", "0003_creditwallet_billing_cre_organiz_b1e572_idx"),
        ("conversations", "0003_chatrequest_organization_conversation_organization_and_more"),
        ("governance", "0003_alter_retentionrule_unique_together"),
        ("identity", "0005_alter_user_contact_organizationinvite_role_and_more"),
        ("jobs", "0003_alter_job_idempotency_key"),
    ]

    operations = [migrations.RunPython(create_analytics_views, drop_analytics_views)]
