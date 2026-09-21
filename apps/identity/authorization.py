from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.db.models import Q, QuerySet
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied

from apps.identity.models import Organization, UserPermission, UserRole


def organization_ids_for_user(user) -> list[str]:
    if not getattr(user, "is_authenticated", False):
        return []
    return [str(org_id) for org_id in user.organizations.values_list("id", flat=True)]


def primary_organization_for_user(user) -> Organization | None:
    if not getattr(user, "is_authenticated", False):
        return None
    return user.organizations.order_by("user_memberships__created_at").first()


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
    include_personal_owner_rows: bool = True,
) -> QuerySet:
    """Scope a queryset to organizations the user belongs to.

    Legacy rows created before full tenant enforcement may have a null
    organization.  Those rows remain visible only when they also belong to the
    acting owner; cross-tenant access still requires membership.
    """
    org_ids = organization_ids_for_user(user)
    scoped = Q(**{f"{organization_field}_id__in": org_ids})
    if include_personal_owner_rows and owner_field:
        scoped |= Q(**{f"{organization_field}__isnull": True, owner_field: user})
    return queryset.filter(scoped)


def user_has_role(user, role_name: str, organization_id=None) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
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
