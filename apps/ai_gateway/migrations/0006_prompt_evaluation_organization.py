import uuid

import django.db.models.deletion
from django.db import migrations, models


def _legacy_organization(Organization, UserOrganization, user_id, resource_id, kind):
    membership = UserOrganization.objects.filter(user_id=user_id).order_by("created_at").first()
    if membership:
        return membership.organization
    organization = Organization.objects.create(
        name=f"Recovered {kind} {resource_id}",
        slug=f"recovered-{kind.lower()}-{uuid.uuid4().hex[:16]}",
        owner_id=user_id,
    )
    if user_id:
        UserOrganization.objects.get_or_create(user_id=user_id, organization=organization)
    return organization


def backfill_organizations(apps, schema_editor):  # noqa: ARG001
    Evaluation = apps.get_model("ai_gateway", "Evaluation")
    Organization = apps.get_model("identity", "Organization")
    Prompt = apps.get_model("ai_gateway", "Prompt")
    UserOrganization = apps.get_model("identity", "UserOrganization")

    for prompt in Prompt.objects.filter(organization_id=None).iterator():
        prompt.organization = _legacy_organization(
            Organization, UserOrganization, prompt.created_by_id, prompt.id, "prompt"
        )
        prompt.save(update_fields=["organization"])
    for evaluation in Evaluation.objects.filter(organization_id=None).iterator():
        if evaluation.prompt_id:
            evaluation.organization_id = evaluation.prompt.organization_id
        else:
            evaluation.organization = _legacy_organization(
                Organization, UserOrganization, evaluation.created_by_id, evaluation.id, "evaluation"
            )
        evaluation.save(update_fields=["organization"])


def noop_reverse(apps, schema_editor):  # noqa: ARG001
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("ai_gateway", "0005_seed_search_research_policy"),
        ("identity", "0006_seed_default_roles"),
    ]

    operations = [
        migrations.AddField(
            model_name="prompt",
            name="organization",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="prompts",
                to="identity.organization",
            ),
        ),
        migrations.AddField(
            model_name="evaluation",
            name="organization",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="evaluations",
                to="identity.organization",
            ),
        ),
        migrations.RunPython(backfill_organizations, noop_reverse),
        migrations.AlterField(
            model_name="prompt",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="prompts",
                to="identity.organization",
            ),
        ),
        migrations.AlterField(
            model_name="evaluation",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="evaluations",
                to="identity.organization",
            ),
        ),
    ]
