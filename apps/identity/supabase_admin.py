"""Server-side Supabase Auth Admin API client (uses the server-only secret key)."""

from __future__ import annotations

import httpx
from django.conf import settings


class SupabaseAdminError(RuntimeError):
    """Supabase rejected or could not complete an admin request."""


def _admin_headers() -> dict[str, str]:
    key = settings.SUPABASE_SECRET_KEY
    if not settings.SUPABASE_URL or not key:
        raise SupabaseAdminError("SUPABASE_URL and SUPABASE_SECRET_KEY are required for admin calls.")
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def delete_auth_user(supabase_user_id: str) -> None:
    """Delete the user from Supabase Auth, the identity source of truth.

    A 404 means the user is already gone and is treated as success so the
    local cleanup stays idempotent.
    """
    url = f"{settings.SUPABASE_URL.rstrip('/')}/auth/v1/admin/users/{supabase_user_id}"
    try:
        response = httpx.delete(url, headers=_admin_headers(), timeout=10.0)
    except httpx.HTTPError as exc:
        raise SupabaseAdminError(f"Supabase admin request failed: {exc}") from exc
    if response.status_code not in (200, 204, 404):
        raise SupabaseAdminError(f"Supabase refused user deletion (HTTP {response.status_code}).")
