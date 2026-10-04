from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.db.models import Q, QuerySet
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied

from apps.identity.models import Organization, Role, UserPermission, UserRole


def organization_ids_for_user(user) -> list[str]:
    if not getattr(user, "is_authenticated", False):
        return []
    return [str(org_id) for org_id in user.organizations.values_list("id", flat=True)]


def primary_organization_for_user(user) -> Organization | None:
    if not getattr(user, "is_authenticated", False):
        return None
    return user.organizations.order_by("user_memberships__created_at").first()


def organization_for_request(request, *, required: bool = False) -> Organization | None:
    """Resolve the organization selected by an authenticated API request.

    ``X-Organization-ID`` lets members of more than one organization choose
    their tenant explicitly. The legacy primary organization is used only when
    the header is absent, preserving single-tenant clients while allowing a
    non-primary organization to be selected.
    """
    organization_id = request.headers.get("X-Organization-ID")
    if organization_id:
        try:
            organization = Organization.objects.get(id=organization_id)
        except Organization.DoesNotExist, ValueError:
            raise PermissionDenied("The selected organization does not exist.") from None
        require_organization_membership(request.user, organization.id)
        return organization

    organization = primary_organization_for_user(request.user)
    if required and organization is None:
        raise PermissionDenied("An organization membership is required for this action.")
    return organization


def require_organization_membership(user, organization_id) -> None:
    if not getattr(user, "is_authenticated", False):
        raise PermissionDenied("Authentication is required.")
    if not user.organizations.filter(id=organization_id).exists():
        raise PermissionDenied("You do not have access to this organization.")


def tenant_scoped_queryset(
    queryset: QuerySet,
    user,
    *,
    organization_field: str = "organization",
    owner_field: str | None = "owner",
    include_personal_owner_rows: bool = False,
    organization_id=None,
) -> QuerySet:
    """Scope a queryset to organizations the user belongs to.

    Null-organization rows are deliberately not exposed by default: an owner
    relationship is not a tenant boundary. The optional legacy flag exists only
    for explicit, audited data-recovery paths.
    """
    org_ids = organization_ids_for_user(user)
    if organization_id is not None:
        require_organization_membership(user, organization_id)
        org_ids = [str(organization_id)]
    scoped = Q(**{f"{organization_field}_id__in": org_ids})
    if include_personal_owner_rows and owner_field:
        scoped |= Q(**{f"{organization_field}__isnull": True, owner_field: user})
    return queryset.filter(scoped)


def user_has_role(user, role_name: str, organization_id=None) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if (
        organization_id is not None
        and role_name == Role.RoleType.ADMIN
        and Organization.objects.filter(id=organization_id, owner=user).exists()
    ):
        return True
    roles = UserRole.objects.filter(user=user, role__name=role_name)
    if organization_id is not None:
        roles = roles.filter(Q(organization_id=organization_id) | Q(organization__isnull=True))
    return roles.exists()


def user_has_permission(user, permission_codename: str, organization_id=None) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    permissions_qs = UserPermission.objects.filter(user=user, permission__codename=permission_codename)
    if organization_id is not None:
        permissions_qs = permissions_qs.filter(
            Q(organization_id=organization_id) | Q(organization__isnull=True)
        )
    return permissions_qs.exists() or user.has_perm(permission_codename)


def user_can_edit_organization(user, organization_id) -> bool:
    """Editors and admins may mutate tenant-owned resources."""
    return user_has_role(user, Role.RoleType.ADMIN, organization_id) or user_has_role(
        user, Role.RoleType.EDITOR, organization_id
    )


def require_organization_write_access(user, organization_id) -> None:
    if not user_can_edit_organization(user, organization_id):
        raise PermissionDenied("Editor or admin access is required for this organization.")


def organization_id_for_object(obj) -> object | None:
    """Find an object's tenant without accepting an owner as a boundary.

    Several tenant-owned resources (knowledge sources/documents and webhook
    deliveries) carry their organization through a parent relation. Keeping the
    lookup here ensures write permissions behave consistently for DRF object
    actions without widening access based on the resource owner.
    """
    organization_id = getattr(obj, "organization_id", None)
    if organization_id is not None:
        return organization_id

    collection = getattr(obj, "collection", None)
    if collection is not None and getattr(collection, "organization_id", None) is not None:
        return collection.organization_id

    source = getattr(obj, "source", None)
    if source is not None:
        source_collection = getattr(source, "collection", None)
        if source_collection is not None:
            return getattr(source_collection, "organization_id", None)

    job = getattr(obj, "job", None)
    if job is not None:
        return getattr(job, "organization_id", None)
    return None


def membership_required(
    organization_id_getter: Callable[..., Any],
) -> Callable:
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            request = args[1] if len(args) > 1 else kwargs.get("request")
            organization_id = organization_id_getter(*args, **kwargs)
            require_organization_membership(request.user, organization_id)
            return func(*args, **kwargs)

        return wrapper

    return decorator


class IsOrganizationMember(permissions.BasePermission):
    """DRF permission for views that expose an `organization_id` kwarg."""

    def has_permission(self, request, view) -> bool:
        organization_id = view.kwargs.get("organization_id")
        if organization_id is None:
            return bool(request.user and request.user.is_authenticated)
        return request.user.organizations.filter(id=organization_id).exists()


class HasOrganizationWriteAccess(permissions.BasePermission):
    """Require an editor or admin role for unsafe operations.

    Object checks use the object's tenant; create checks use the request's
    selected tenant. Read access is supplied by tenant-scoped querysets.
    """

    def has_permission(self, request, view) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        if getattr(view, "action", None) in {None, "create"}:
            organization = organization_for_request(request, required=True)
            return user_can_edit_organization(request.user, organization.id)
        return True

    def has_object_permission(self, request, view, obj) -> bool:
        organization_id = organization_id_for_object(obj)
        if organization_id is None:
            return False
        if request.method in permissions.SAFE_METHODS:
            return request.user.organizations.filter(id=organization_id).exists()
        return user_can_edit_organization(request.user, organization_id)


class IsOrganizationAdmin(permissions.BasePermission):
    """Governance surfaces (tool policies, credentials, MCP servers, the audit log) are admin-only."""

    def has_permission(self, request, view) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False
        organization = organization_for_request(request, required=True)
        return user_has_role(request.user, Role.RoleType.ADMIN, organization.id)
