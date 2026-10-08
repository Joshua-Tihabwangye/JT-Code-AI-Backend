"""Server-side Supabase Auth Admin API client (uses the server-only secret key)."""

from __future__ import annotations

import httpx
from django.conf import settings


class SupabaseAdminError(RuntimeError):
    """Supabase rejected or could not complete an admin request."""


def _supabase_api_base_url() -> str:
    return (settings.SUPABASE_INTERNAL_URL or settings.SUPABASE_URL).rstrip("/")


def _admin_headers() -> dict[str, str]:
    key = settings.SUPABASE_SECRET_KEY
    if not _supabase_api_base_url() or not key:
        raise SupabaseAdminError("Supabase API URL and SUPABASE_SECRET_KEY are required for admin calls.")
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def delete_auth_user(supabase_user_id: str) -> None:
    """Delete the user from Supabase Auth, the identity source of truth.

    A 404 means the user is already gone and is treated as success so the
    local cleanup stays idempotent.
    """
    url = f"{_supabase_api_base_url()}/auth/v1/admin/users/{supabase_user_id}"
    try:
        response = httpx.delete(url, headers=_admin_headers(), timeout=10.0)
    except httpx.HTTPError as exc:
        raise SupabaseAdminError(f"Supabase admin request failed: {exc}") from exc
    if response.status_code not in (200, 204, 404):
        raise SupabaseAdminError(f"Supabase refused user deletion (HTTP {response.status_code}).")


def verify_password(email: str, password: str) -> bool:
    """True when Supabase Auth accepts ``email``/``password`` (password grant)."""
    url = f"{_supabase_api_base_url()}/auth/v1/token?grant_type=password"
    try:
        response = httpx.post(
            url, json={"email": email, "password": password}, headers=_admin_headers(), timeout=10.0
        )
    except httpx.HTTPError as exc:
        raise SupabaseAdminError(f"Supabase auth request failed: {exc}") from exc
    if response.status_code == 200:
        return True
    if response.status_code in (400, 401, 422):
        return False
    raise SupabaseAdminError(f"Supabase password check failed (HTTP {response.status_code}).")


def set_password(supabase_user_id: str, password: str) -> None:
    url = f"{_supabase_api_base_url()}/auth/v1/admin/users/{supabase_user_id}"
    try:
        response = httpx.put(url, json={"password": password}, headers=_admin_headers(), timeout=10.0)
    except httpx.HTTPError as exc:
        raise SupabaseAdminError(f"Supabase admin request failed: {exc}") from exc
    if response.status_code == 422:
        raise ValueError(response.json().get("msg") or "The new password was rejected.")
    if response.status_code >= 400:
        raise SupabaseAdminError(f"Supabase refused the password update (HTTP {response.status_code}).")
