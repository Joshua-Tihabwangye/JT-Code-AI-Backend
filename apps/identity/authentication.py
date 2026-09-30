from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
from django.conf import settings
from rest_framework import authentication, exceptions

from apps.identity.models import User

_JWKS_CACHE: dict[str, Any] | None = None
_JWKS_FETCHED_AT = 0.0
_JWKS_TTL_SECONDS = 3600
# A token with an unknown ``kid`` forces a refresh; cap how often that can
# happen so forged tokens cannot turn every request into a Supabase round trip.
_JWKS_MIN_REFRESH_INTERVAL_SECONDS = 60


def _fetch_jwks(*, force_refresh: bool = False) -> dict[str, Any]:
    global _JWKS_CACHE, _JWKS_FETCHED_AT
    now = time.monotonic()
    fresh = _JWKS_CACHE is not None and now - _JWKS_FETCHED_AT < _JWKS_TTL_SECONDS
    recently_fetched = _JWKS_CACHE is not None and now - _JWKS_FETCHED_AT < _JWKS_MIN_REFRESH_INTERVAL_SECONDS
    if fresh and (not force_refresh or recently_fetched):
        return _JWKS_CACHE  # type: ignore[return-value]

    jwks_url = settings.SUPABASE_JWKS_URL or (
        settings.SUPABASE_URL.rstrip("/") + "/auth/v1/.well-known/jwks.json"
    )
    with httpx.Client(timeout=10.0) as client:
        response = client.get(jwks_url)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
        raise ValueError("Invalid JWKS payload from Supabase.")
    _JWKS_CACHE = payload
    _JWKS_FETCHED_AT = now
    return payload


def _verify_with_jwks(token: str) -> dict[str, Any]:
    from jwt import PyJWK

    jwks = _fetch_jwks()
    unverified_header = jwt.get_unverified_header(token)
    kid = unverified_header.get("kid")
    key = None
    if kid:
        key = next((k for k in jwks["keys"] if k.get("kid") == kid), None)
    if key is None:
        # Supabase may rotate signing keys before the normal cache TTL. Refresh
        # once for an unknown key while continuing to fail closed on errors.
        jwks = _fetch_jwks(force_refresh=True)
        if kid:
            key = next((candidate for candidate in jwks["keys"] if candidate.get("kid") == kid), None)
        if key is None:
            raise ValueError("No matching JWKS key for token.")

    algorithm = unverified_header.get("alg")
    if algorithm not in {"ES256", "RS256"} or key.get("alg", algorithm) != algorithm:
        raise ValueError("Unsupported JWKS token algorithm.")

    return jwt.decode(
        token,
        PyJWK.from_dict(key).key,
        algorithms=[algorithm],
        audience=settings.SUPABASE_JWT_AUDIENCE or "authenticated",
        issuer=settings.SUPABASE_JWT_ISSUER or None,
        options={"verify_aud": bool(settings.SUPABASE_JWT_AUDIENCE)},
        leeway=30,
    )


def _verify_with_secret(token: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        settings.SUPABASE_JWT_SECRET,
        algorithms=["HS256"],
        audience=settings.SUPABASE_JWT_AUDIENCE or None,
        issuer=settings.SUPABASE_JWT_ISSUER or None,
        options={"verify_aud": bool(settings.SUPABASE_JWT_AUDIENCE)},
        leeway=30,
    )


class SupabaseJWTAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header:
            return None
        if len(header) != 2 or header[0].decode().lower() != self.keyword.lower():
            raise exceptions.AuthenticationFailed("Invalid authorization header.")
        token = header[1].decode()

        claims: dict[str, Any] | None = None
        last_error: Exception | None = None
        if settings.SUPABASE_URL:
            try:
                claims = _verify_with_jwks(token)
            except jwt.ExpiredSignatureError:
                raise exceptions.AuthenticationFailed("Supabase token has expired.") from None
            except Exception as exc:
                last_error = exc
            if claims is None:
                raise exceptions.AuthenticationFailed(
                    "Invalid or expired Supabase session token."
                ) from last_error
        elif settings.SUPABASE_JWT_SECRET:
            try:
                claims = _verify_with_secret(token)
            except jwt.ExpiredSignatureError:
                raise exceptions.AuthenticationFailed("Supabase token has expired.") from None
            except Exception as exc:
                last_error = exc
        if claims is None:
            raise exceptions.AuthenticationFailed(
                "Invalid or expired Supabase session token."
            ) from last_error

        subject = claims.get("sub")
        if not subject:
            raise exceptions.AuthenticationFailed("Supabase token is missing subject.")
        # Supabase is the identity source of truth: only signed-in user sessions
        # (role=authenticated) are accepted, never anon/service-role tokens.
        if claims.get("role") != "authenticated":
            raise exceptions.AuthenticationFailed("Supabase token is not an authenticated user session.")
        if claims.get("is_anonymous") and not settings.SUPABASE_ALLOW_ANONYMOUS_USERS:
            raise exceptions.AuthenticationFailed("Anonymous Supabase sessions are not permitted.")

        email = claims.get("email") if isinstance(claims.get("email"), str) else ""
        user, created = User.objects.get_or_create(supabase_user_id=subject, defaults={"email": email})
        if not user.is_active:
            raise exceptions.AuthenticationFailed("This user account is disabled.")
        if not created and email and user.email != email:
            User.objects.filter(pk=user.pk).update(email=email)
            user.email = email
        return user, claims

    def authenticate_header(self, request) -> str:
        return self.keyword
