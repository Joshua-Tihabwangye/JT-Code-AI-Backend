"""Release legacy holds and make the usage records and credit ledger append-only.

Before Phase 13, credit holds lived only in ``CreditWallet.reserved_balance``
and several code paths never released them. No ``UsageReservation`` tracks
those holds, so they are returned to the wallets here. The triggers then
reject UPDATE/DELETE on ``usage_usagerecord`` and ``billing_creditledger``
except (a) clearing the nullable user/reservation links on a usage record and
(b) inside a transaction that set ``jt_code.ledger_purge`` (organization
deletion). TRUNCATE (used by test flushes) does not fire row triggers.
"""

from django.db import migrations

FUNCTION = """
CREATE OR REPLACE FUNCTION jt_code_append_only() RETURNS trigger AS $$
BEGIN
    IF current_setting('jt_code.ledger_purge', true) = 'on' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'usage_usagerecord'
       AND (to_jsonb(NEW) - 'user_id' - 'reservation_id') = (to_jsonb(OLD) - 'user_id' - 'reservation_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'append-only ledger: % on % is not allowed', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;
"""
TABLES = ("usage_usagerecord", "billing_creditledger")


def release_legacy_holds(apps, schema_editor):  # noqa: ARG001
    CreditWallet = apps.get_model("billing", "CreditWallet")
    CreditWallet.objects.exclude(reserved_balance=0).update(reserved_balance=0)


def install(apps, schema_editor):  # noqa: ARG001
    # A raw cursor without params: psycopg would treat RAISE's % markers as placeholders.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(FUNCTION)
        for table in TABLES:
            cursor.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
            cursor.execute(
                f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION jt_code_append_only()"
            )


def uninstall(apps, schema_editor):  # noqa: ARG001
    for table in TABLES:
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    schema_editor.execute("DROP FUNCTION IF EXISTS jt_code_append_only()")


class Migration(migrations.Migration):
    dependencies = [
        ("usage", "0001_initial"),
        ("billing", "0004_metering_fields"),
    ]

    operations = [
        migrations.RunPython(release_legacy_holds, migrations.RunPython.noop),
        migrations.RunPython(install, uninstall),
    ]
