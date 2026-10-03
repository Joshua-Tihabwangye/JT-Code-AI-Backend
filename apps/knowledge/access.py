"""Authorization policy for document and chunk retrieval.

Tenant membership is necessary but not sufficient for restricted documents.
This module is the single policy boundary used by API querysets, lexical
retrieval, pgvector retrieval, citation persistence, and evaluation.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.identity.authorization import user_has_role
from apps.identity.models import Role


def document_access_q(user, *, prefix: str = "") -> Q:
    """Return the row-level document policy for an authenticated principal."""
    if not getattr(user, "is_authenticated", False):
        return Q(pk__in=[])

    visibility = f"{prefix}visibility"
    creator = f"{prefix}source__created_by_id"
    grants = f"{prefix}access_grants__user_id"
    return Q(**{visibility: "organization"}) | Q(**{creator: user.id}) | Q(**{grants: user.id})


def accessible_documents(queryset: QuerySet, user, *, organization_id=None) -> QuerySet:
    """Apply tenant and document ACL filters to a Document queryset."""
    if not getattr(user, "is_authenticated", False):
        return queryset.none()
    if organization_id is not None:
        queryset = queryset.filter(collection__organization_id=organization_id)
        if user_has_role(user, Role.RoleType.ADMIN, organization_id):
            return queryset
    return queryset.filter(document_access_q(user)).distinct()


def accessible_chunks(queryset: QuerySet, user, *, organization_id=None) -> QuerySet:
    """Apply tenant and parent-document ACL filters to a Chunk queryset."""
    if not getattr(user, "is_authenticated", False):
        return queryset.none()
    if organization_id is not None:
        queryset = queryset.filter(collection__organization_id=organization_id)
        if user_has_role(user, Role.RoleType.ADMIN, organization_id):
            return queryset
    return queryset.filter(document_access_q(user, prefix="document__")).distinct()


def normalize_acl(raw_acl: object) -> tuple[str, list[str]]:
    """Validate the ingestion ACL contract and return visibility plus user ids.

    Accepted form::

        {"visibility": "restricted", "user_ids": ["<uuid>", ...]}

    The default is organization-visible. Unknown keys are rejected so a typo
    cannot accidentally widen access.
    """
    if raw_acl in (None, {}):
        return "organization", []
    if not isinstance(raw_acl, dict):
        raise ValueError("acl must be an object.")
    unknown = set(raw_acl) - {"visibility", "user_ids"}
    if unknown:
        raise ValueError(f"Unsupported acl keys: {', '.join(sorted(unknown))}.")
    visibility = raw_acl.get("visibility", "organization")
    if visibility not in {"organization", "restricted"}:
        raise ValueError("acl.visibility must be organization or restricted.")
    user_ids = raw_acl.get("user_ids", [])
    if not isinstance(user_ids, list) or not all(isinstance(value, str) for value in user_ids):
        raise ValueError("acl.user_ids must be a list of user UUID strings.")
    if visibility == "organization" and user_ids:
        raise ValueError("acl.user_ids is only valid for restricted documents.")
    return visibility, list(dict.fromkeys(user_ids))
