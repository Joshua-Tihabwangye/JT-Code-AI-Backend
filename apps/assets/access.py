"""Single authorization policy for raw asset bytes and metadata.

An asset is a raw byte channel, so tenant membership alone is not enough: the
bytes of a private upload may back a restricted knowledge document or a private
dataset. Private assets are visible to their owner and organization admins;
organization-visible assets to every member. Consumers expose derived content
(chunks, analysis results) under their own policies.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet

from apps.assets.models import Asset
from apps.identity.authorization import organization_ids_for_user, user_has_role
from apps.identity.models import Role


def _is_admin(user: Any, organization_id: Any) -> bool:
    return user_has_role(user, Role.RoleType.ADMIN, organization_id)


def assets_visible_to(user: Any, organization_id: Any = None) -> QuerySet[Asset]:
    """Assets the user may read in one organization (or all of their organizations)."""
    if not getattr(user, "is_authenticated", False):
        return Asset.objects.none()
    org_ids = [str(organization_id)] if organization_id is not None else organization_ids_for_user(user)
    member_orgs = {str(value) for value in organization_ids_for_user(user)}
    org_ids = [value for value in org_ids if str(value) in member_orgs]
    if not org_ids:
        return Asset.objects.none()
    admin_orgs = [value for value in org_ids if _is_admin(user, value)]
    member_rule = Q(organization_id__in=org_ids) & (
        Q(owner=user) | Q(visibility=Asset.Visibility.ORGANIZATION)
    )
    rule = Q(organization_id__in=admin_orgs) | member_rule
    return Asset.objects.filter(rule)


def can_read_asset(user: Any, asset: Asset) -> bool:
    return assets_visible_to(user, asset.organization_id).filter(id=asset.id).exists()


def can_manage_asset(user: Any, asset: Asset) -> bool:
    """Owners and organization admins may rename, re-share or delete an asset."""
    if not getattr(user, "is_authenticated", False):
        return False
    if not user.organizations.filter(id=asset.organization_id).exists():
        return False
    return asset.owner_id == user.id or _is_admin(user, asset.organization_id)
