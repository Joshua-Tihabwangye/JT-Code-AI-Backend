from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver

from apps.identity.models import Role, User, UserOrganization, UserRole

_ROLE_DESCRIPTIONS = {
    Role.RoleType.ADMIN: "Can administer an organization and its resources.",
    Role.RoleType.EDITOR: "Can create and modify organization resources.",
    Role.RoleType.VIEWER: "Can view organization resources.",
}


def _assign_default_organization_role(membership: UserOrganization) -> None:
    """Give a membership an explicit tenant-scoped baseline role."""
    role_name = (
        Role.RoleType.ADMIN
        if membership.organization.owner_id == membership.user_id
        else Role.RoleType.VIEWER
    )
    role, _ = Role.objects.get_or_create(
        name=role_name,
        defaults={"description": _ROLE_DESCRIPTIONS[role_name]},
    )
    UserRole.objects.get_or_create(
        user=membership.user,
        role=role,
        organization=membership.organization,
    )


@receiver(post_save, sender=UserOrganization)
def assign_default_organization_role(sender, instance, created, **kwargs):
    """Cover direct creation of the custom through model."""
    if created:
        _assign_default_organization_role(instance)


@receiver(m2m_changed, sender=User.organizations.through)
def assign_roles_for_membership_add(sender, instance, action, reverse, model, pk_set, **kwargs):
    """Cover ``user.organizations.add(...)``, which bulk-creates through rows."""
    if action != "post_add" or not pk_set:
        return
    if reverse:
        memberships = UserOrganization.objects.filter(organization=instance, user_id__in=pk_set)
    else:
        memberships = UserOrganization.objects.filter(user=instance, organization_id__in=pk_set)
    for membership in memberships.select_related("organization", "user"):
        _assign_default_organization_role(membership)
