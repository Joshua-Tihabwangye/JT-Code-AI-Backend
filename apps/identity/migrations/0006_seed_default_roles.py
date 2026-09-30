from django.db import migrations


ROLE_DESCRIPTIONS = {
    "admin": "Full administrative access within an organization.",
    "editor": "May create and modify organization resources.",
    "viewer": "May view organization resources.",
}


def seed_default_roles(apps, schema_editor):  # noqa: ARG001
    Organization = apps.get_model("identity", "Organization")
    Role = apps.get_model("identity", "Role")
    UserOrganization = apps.get_model("identity", "UserOrganization")
    UserRole = apps.get_model("identity", "UserRole")

    roles = {
        name: Role.objects.get_or_create(name=name, defaults={"description": description})[0]
        for name, description in ROLE_DESCRIPTIONS.items()
    }
    for organization in Organization.objects.exclude(owner_id=None):
        UserRole.objects.get_or_create(
            user_id=organization.owner_id,
            role=roles["admin"],
            organization=organization,
        )
    for membership in UserOrganization.objects.iterator():
        if not UserRole.objects.filter(user_id=membership.user_id, organization=membership.organization).exists():
            UserRole.objects.create(
                user_id=membership.user_id,
                role=roles["viewer"],
                organization=membership.organization,
            )


def unseed_default_roles(apps, schema_editor):  # noqa: ARG001
    Role = apps.get_model("identity", "Role")
    Role.objects.filter(name__in=ROLE_DESCRIPTIONS).delete()


class Migration(migrations.Migration):
    dependencies = [("identity", "0005_alter_user_contact_organizationinvite_role_and_more")]

    operations = [migrations.RunPython(seed_default_roles, unseed_default_roles)]
