"""Phase 15 audit pipeline: platform-level events and an append-only audit table.

Security events that are not tied to a tenant (a forged webhook signature, for
example) have no organization, so the column becomes nullable. The trigger
rejects UPDATE/DELETE on ``governance_auditevent`` except (a) clearing the
actor when a user is deleted (``SET NULL``) and (b) inside a transaction that
set ``jt_code.ledger_purge`` - organization deletion and the retention task.
"""

import django.db.models.deletion
from django.db import migrations, models

FUNCTION = """
CREATE OR REPLACE FUNCTION jt_code_audit_append_only() RETURNS trigger AS $$
BEGIN
    IF current_setting('jt_code.ledger_purge', true) = 'on' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'UPDATE' AND (to_jsonb(NEW) - 'actor_id') = (to_jsonb(OLD) - 'actor_id')
       AND NEW.actor_id IS NULL THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'append-only audit log: % on % is not allowed', TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;
"""


def install(apps, schema_editor):  # noqa: ARG001
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(FUNCTION)
        cursor.execute("DROP TRIGGER IF EXISTS governance_auditevent_append_only ON governance_auditevent")
        cursor.execute(
            "CREATE TRIGGER governance_auditevent_append_only BEFORE UPDATE OR DELETE ON governance_auditevent "
            "FOR EACH ROW EXECUTE FUNCTION jt_code_audit_append_only()"
        )


def uninstall(apps, schema_editor):  # noqa: ARG001
    schema_editor.execute("DROP TRIGGER IF EXISTS governance_auditevent_append_only ON governance_auditevent")
    schema_editor.execute("DROP FUNCTION IF EXISTS jt_code_audit_append_only()")


class Migration(migrations.Migration):
    dependencies = [("governance", "0011_lock_down_supabase_data_api"), ("identity", "__first__")]

    operations = [
        migrations.AlterField(
            model_name="auditevent",
            name="organization",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="audit_events",
                to="identity.organization",
            ),
        ),
        migrations.AddField(
            model_name="auditevent",
            name="outcome",
            field=models.CharField(
                choices=[("success", "Success"), ("denied", "Denied"), ("failure", "Failure")],
                default="success",
                max_length=10,
            ),
        ),
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(fields=["severity", "-created_at"], name="governance_audit_sev_idx"),
        ),
        migrations.RunPython(install, uninstall),
    ]
