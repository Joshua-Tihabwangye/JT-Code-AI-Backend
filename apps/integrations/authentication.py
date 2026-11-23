"""Organization API keys (``X-API-Key``) for server-to-server calls.

A key acts as the user who created it, inside the key's organization, with these limits (fail closed):

* only an **active**, unexpired key of an active user who is still a member of the key's organization;
* the request is pinned to the key's organization: a different ``X-Organization-ID`` is refused;
* **scopes**: ``read`` allows safe methods only; ``write`` is required for anything else (a key with no
  scope can only read);
* a key can never manage keys, the account, sessions, members, invites or tool credentials (a leaked key
  must not be able to persist itself or take over the account);
* each key has its own hourly limit (``rate_limit``, capped by ``API_KEY_MAX_RATE_LIMIT``); unknown keys
  count towards the per-IP failure lockout shared with bearer tokens.

Keys are stored as SHA-256 digests only; the full key is shown once, at creation.
"""

from __future__ import annotations

import hashlib

from django.conf import settings
from django.core.cache import caches
from django.utils import timezone
from rest_framework import authentication, exceptions

API_KEY_HEADER = "X-API-Key"
SCOPES = ("read", "write")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# A key may never call these: credential, account and membership management.
FORBIDDEN_PREFIXES = (
    "/api/v1/api-keys/",
    "/api/v1/accounts/",
    "/api/v1/settings/",
    "/api/v1/organizations/",
    "/api/v1/tool-credentials/",
)


def key_digest(full_key: str) -> str:
    return hashlib.sha256(full_key.encode()).hexdigest()


class APIKeyAuthentication(authentication.BaseAuthentication):
    def authenticate(self, request):
        supplied = request.headers.get(API_KEY_HEADER, "").strip()
        if not supplied:
            return None
        from apps.identity.authentication import _FAILURE_LOCK_KEY, _record_failure, _request_ip

        ip = _request_ip(request)
        if caches["rate_limits"].get(_FAILURE_LOCK_KEY.format(ip=ip)):
            raise exceptions.Throttled(
                wait=settings.AUTH_FAILURE_WINDOW_SECONDS, detail="Too many invalid keys."
            )
        if len(supplied) > 200:
            _record_failure(ip)
            raise exceptions.AuthenticationFailed("Invalid API key.")

        from apps.integrations.models import APIKey

        api_key = (
            APIKey.objects.select_related("user", "organization")
            .filter(key_hash=key_digest(supplied), status=APIKey.Status.ACTIVE)
            .first()
        )
        if api_key is None:
            _record_failure(ip)
            raise exceptions.AuthenticationFailed("Invalid API key.")
        if api_key.expires_at and api_key.expires_at <= timezone.now():
            APIKey.objects.filter(pk=api_key.pk).update(
                status=APIKey.Status.EXPIRED, updated_at=timezone.now()
            )
            raise exceptions.AuthenticationFailed("This API key has expired.")
        user = api_key.user
        if not user.is_active:
            raise exceptions.AuthenticationFailed("Invalid API key.")

        if request.path.startswith(FORBIDDEN_PREFIXES):
            raise exceptions.PermissionDenied("API keys cannot manage keys, accounts or members.")
        if request.method not in _SAFE_METHODS and "write" not in set(api_key.scopes or ()):
            raise exceptions.PermissionDenied("This API key is read-only (scope 'write' is required).")

        # Pin the tenant: everything downstream resolves the key's organization.
        selected = request.headers.get("X-Organization-ID")
        if selected and selected != str(api_key.organization_id):
            raise exceptions.PermissionDenied("This API key belongs to another organization.")
        request._request.META["HTTP_X_ORGANIZATION_ID"] = str(api_key.organization_id)
        from apps.identity.authorization import require_organization_membership

        require_organization_membership(user, api_key.organization_id)

        from apps.core.ratelimit import hit

        limit = max(1, min(int(api_key.rate_limit or 1), settings.API_KEY_MAX_RATE_LIMIT))
        if not hit(f"api-key:{api_key.pk}", limit=limit, window=3600).allowed:
            raise exceptions.Throttled(detail="This API key's hourly limit is used up.")

        # Record use at most once a minute per key, not on every request.
        if caches["rate_limits"].add(f"api-key-used:{api_key.pk}", True, 60):
            APIKey.objects.filter(pk=api_key.pk).update(last_used_at=timezone.now())
        request._request.api_key = api_key
        return user, api_key

    def authenticate_header(self, request) -> str:
        return API_KEY_HEADER
