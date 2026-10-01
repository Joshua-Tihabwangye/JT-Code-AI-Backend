"""Tenant-scoped credential lookup for adapters (decrypts only at the point of use)."""

from __future__ import annotations

from typing import Any

from apps.tools.crypto import decrypt_secret
from apps.tools.gateway import ToolDenied
from apps.tools.models import ToolCredential


def credential_for(
    organization_id: Any, provider: str, name: str | None = None
) -> tuple[ToolCredential, str]:
    """Return the tenant's active credential and its decrypted secret.

    Lookups are always filtered by the calling tenant, so another tenant's
    credential can never be selected by name.
    """
    credentials = ToolCredential.objects.filter(
        organization_id=organization_id, provider=provider, is_active=True
    )
    if name:
        credentials = credentials.filter(name=name)
    credential = credentials.order_by("created_at").first()
    if credential is None:
        raise ToolDenied(
            "NO_CREDENTIAL", f"No active {provider} connection is configured for this organization."
        )
    return credential, decrypt_secret(credential.encrypted_secret)
