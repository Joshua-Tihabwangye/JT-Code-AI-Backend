"""Backfill tenant ownership for rows created before tenant enforcement."""

import uuid

from django.db import migrations


def _organization_for_owner(Organization, UserOrganization, owner_id, *, resource, resource_id):
    membership = UserOrganization.objects.filter(user_id=owner_id).order_by("created_at").first()
    if membership:
        return membership.organization_id

    # Legacy rows must not remain permanently inaccessible.  Where an owner
    # has no membership, recover the row into a dedicated, owner-backed tenant.
    organization = Organization.objects.create(
        name=f"Recovered {resource} {resource_id}",
        slug=f"recovered-{resource.lower()}-{uuid.uuid4().hex[:16]}",
        owner_id=owner_id,
    )
    if owner_id:
        UserOrganization.objects.get_or_create(user_id=owner_id, organization_id=organization.id)
    return organization.id


def backfill_legacy_tenant_rows(apps, schema_editor):  # noqa: ARG001
    Organization = apps.get_model("identity", "Organization")
    UserOrganization = apps.get_model("identity", "UserOrganization")
    Asset = apps.get_model("assets", "Asset")
    Conversation = apps.get_model("conversations", "Conversation")
    Message = apps.get_model("conversations", "Message")
    ChatRequest = apps.get_model("conversations", "ChatRequest")
    Document = apps.get_model("documents", "Document")
    ConversionJob = apps.get_model("conversions", "ConversionJob")
    Job = apps.get_model("jobs", "Job")

    owner_models = (
        (Asset, "Asset"),
        (Conversation, "Conversation"),
        (Document, "Document"),
        (ConversionJob, "ConversionJob"),
        (Job, "Job"),
    )
    for model, resource in owner_models:
        for row in model.objects.filter(organization_id__isnull=True).iterator():
            row.organization_id = _organization_for_owner(
                Organization, UserOrganization, row.owner_id, resource=resource, resource_id=row.id
            )
            row.save(update_fields=["organization"])

    # Dependent conversation rows inherit their conversation tenant.  The
    # fallback covers historical corruption where the parent was deleted.
    for row in Message.objects.filter(organization_id__isnull=True).select_related("conversation").iterator():
        row.organization_id = row.conversation.organization_id
        row.save(update_fields=["organization"])
    for row in ChatRequest.objects.filter(organization_id__isnull=True).select_related("conversation").iterator():
        row.organization_id = row.conversation.organization_id or _organization_for_owner(
            Organization, UserOrganization, row.owner_id, resource="ChatRequest", resource_id=row.id
        )
        row.save(update_fields=["organization"])


def noop_reverse(apps, schema_editor):  # noqa: ARG001
    # Assignment is intentionally irreversible: the previous NULL value has no
    # tenant meaning and restoring it would recreate inaccessible records.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("ai_gateway", "0007_tenant_slug_constraints"),
        ("assets", "0003_asset_organization"),
        ("conversations", "0003_chatrequest_organization_conversation_organization_and_more"),
        ("conversions", "0001_initial"),
        ("documents", "0001_initial"),
        ("governance", "0005_analytics_readonly_role"),
        ("identity", "0006_seed_default_roles"),
        ("jobs", "0003_alter_job_idempotency_key"),
    ]

    operations = [migrations.RunPython(backfill_legacy_tenant_rows, noop_reverse)]
