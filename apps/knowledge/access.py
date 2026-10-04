"""Authorization policy for document and chunk retrieval.

Tenant membership is necessary but not sufficient for restricted documents.
This module is the single policy boundary used by API querysets, lexical
retrieval, pgvector retrieval, citation persistence, and evaluation.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Exists, OuterRef, Q, QuerySet

from apps.identity.authorization import user_has_role
from apps.identity.models import Role


def document_access_q(user: Any, *, prefix: str = "") -> Q:
    """Return the row-level document policy for an authenticated principal.

    Grants are checked with ``EXISTS`` rather than a join so the policy never
    multiplies rows; callers need no ``DISTINCT`` (which would defeat the
    pgvector HNSW index and full-text ranking).
    """
    if not getattr(user, "is_authenticated", False):
        return Q(pk__in=[])
    from apps.knowledge.models import DocumentAccessGrant

    # ``prefix`` is "" (Document rows) or "document__" (Chunk/Citation rows).
    document_ref = f"{prefix[:-2]}_id" if prefix else "pk"
    granted = Exists(DocumentAccessGrant.objects.filter(document_id=OuterRef(document_ref), user_id=user.id))
    visibility = f"{prefix}visibility"
    creator = f"{prefix}source__created_by_id"
    return Q(**{visibility: "organization"}) | Q(**{creator: user.id}) | Q(granted)


def accessible_documents(queryset: QuerySet[Any], user: Any, *, organization_id: Any = None) -> QuerySet[Any]:
    """Apply tenant and document ACL filters to a Document queryset."""
    if not getattr(user, "is_authenticated", False):
        return queryset.none()
    if organization_id is not None:
        queryset = queryset.filter(collection__organization_id=organization_id)
        if user_has_role(user, Role.RoleType.ADMIN, organization_id):
            return queryset
    return queryset.filter(document_access_q(user))


def accessible_chunks(queryset: QuerySet[Any], user: Any, *, organization_id: Any = None) -> QuerySet[Any]:
    """Apply tenant and parent-document ACL filters to a Chunk queryset."""
    if not getattr(user, "is_authenticated", False):
        return queryset.none()
    if organization_id is not None:
        queryset = queryset.filter(collection__organization_id=organization_id)
        if user_has_role(user, Role.RoleType.ADMIN, organization_id):
            return queryset
    return queryset.filter(document_access_q(user, prefix="document__"))


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
    return visibility, list(dict[str, Any].fromkeys(user_ids))
