from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("assets", "0003_asset_organization"),
        ("billing", "0003_creditwallet_billing_cre_organiz_b1e572_idx"),
        ("conversations", "0003_chatrequest_organization_conversation_organization_and_more"),
        ("governance", "0003_alter_retentionrule_unique_together"),
        ("identity", "0005_alter_user_contact_organizationinvite_role_and_more"),
        ("jobs", "0003_alter_job_idempotency_key"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE VIEW IF NOT EXISTS analytics_job_summary AS
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
            reverse_sql="DROP VIEW IF EXISTS analytics_job_summary",
        ),
        migrations.RunSQL(
            sql="""
                CREATE VIEW IF NOT EXISTS analytics_usage_ledger AS
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
            reverse_sql="DROP VIEW IF EXISTS analytics_usage_ledger",
        ),
        migrations.RunSQL(
            sql="""
                CREATE VIEW IF NOT EXISTS analytics_billing_summary AS
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
            reverse_sql="DROP VIEW IF EXISTS analytics_billing_summary",
        ),
        migrations.RunSQL(
            sql="""
                CREATE VIEW IF NOT EXISTS analytics_conversation_summary AS
                SELECT
                    conversation.organization_id AS organization_id,
                    COUNT(DISTINCT conversation.id) AS conversation_count,
                    COUNT(message.id) AS message_count
                FROM conversations_conversation conversation
                LEFT JOIN conversations_message message
                    ON message.conversation_id = conversation.id
                GROUP BY conversation.organization_id
            """,
            reverse_sql="DROP VIEW IF EXISTS analytics_conversation_summary",
        ),
        migrations.RunSQL(
            sql="""
                CREATE VIEW IF NOT EXISTS analytics_asset_summary AS
                SELECT
                    organization_id,
                    resource_type,
                    status,
                    COUNT(*) AS asset_count,
                    SUM(bytes) AS total_bytes
                FROM assets_asset
                GROUP BY organization_id, resource_type, status
            """,
            reverse_sql="DROP VIEW IF EXISTS analytics_asset_summary",
        ),
    ]
