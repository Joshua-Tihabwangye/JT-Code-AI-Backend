from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.cache import caches
from rest_framework import authentication, exceptions

from apps.identity.models import User

_JWKS_CACHE: dict[str, Any] | None = None
_JWKS_FETCHED_AT = 0.0
_JWKS_TTL_SECONDS = 3600
# A token with an unknown ``kid`` forces a refresh; cap how often that can
# happen so forged tokens cannot turn every request into a Supabase round trip.
_JWKS_MIN_REFRESH_INTERVAL_SECONDS = 60
# Supabase user sessions always carry these; the audience is always checked ("authenticated").
_DECODE_OPTIONS = {"require": ["exp", "iat", "sub", "aud", "role"], "verify_aud": True}
# Real Supabase access tokens are ~1 KiB; anything far larger is rejected before parsing.
MAX_TOKEN_BYTES = 8192
_FAILURE_LOCK_KEY = "auth-lock:{ip}"


def _fetch_jwks(*, force_refresh: bool = False) -> dict[str, Any]:
    global _JWKS_CACHE, _JWKS_FETCHED_AT
    now = time.monotonic()
    fresh = _JWKS_CACHE is not None and now - _JWKS_FETCHED_AT < _JWKS_TTL_SECONDS
    recently_fetched = _JWKS_CACHE is not None and now - _JWKS_FETCHED_AT < _JWKS_MIN_REFRESH_INTERVAL_SECONDS
    if fresh and (not force_refresh or recently_fetched):
        return _JWKS_CACHE  # type: ignore[return-value]

    auth_base_url = settings.SUPABASE_INTERNAL_URL or settings.SUPABASE_URL
    jwks_url = settings.SUPABASE_JWKS_URL or (auth_base_url.rstrip("/") + "/auth/v1/.well-known/jwks.json")
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
        options=_DECODE_OPTIONS,
        leeway=30,
    )


def _verify_with_secret(token: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        settings.SUPABASE_JWT_SECRET,
        algorithms=["HS256"],
        audience=settings.SUPABASE_JWT_AUDIENCE or "authenticated",
        issuer=settings.SUPABASE_JWT_ISSUER or None,
        options=_DECODE_OPTIONS,
        leeway=30,
    )


def _request_ip(request) -> str:
    from apps.core.edge import client_ip

    return client_ip(getattr(request, "_request", request))


def _record_failure(ip: str) -> None:
    """Count an invalid token for this IP; past ``AUTH_FAILURE_LIMIT`` per window, lock the IP out.

    During the lockout every bearer-token request from the IP gets 429 without being verified, which
    caps token-fuzzing and garbage-token floods. The limit is generous so shared (NAT) addresses are
    not locked out by ordinary expired-token retries.
    """
    from apps.core.metrics import SECURITY_EVENTS
    from apps.core.ratelimit import hit

    window = settings.AUTH_FAILURE_WINDOW_SECONDS
    decision = hit(f"auth-fail:{ip}", limit=settings.AUTH_FAILURE_LIMIT, window=window)
    if not decision.allowed:
        caches["rate_limits"].set(_FAILURE_LOCK_KEY.format(ip=ip), True, window)
        SECURITY_EVENTS.labels("auth_failure_lockout").inc()


class SupabaseJWTAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header:
            return None
        if len(header) != 2 or header[0].decode(errors="replace").lower() != self.keyword.lower():
            raise exceptions.AuthenticationFailed("Invalid authorization header.")
        ip = _request_ip(request)
        if caches["rate_limits"].get(_FAILURE_LOCK_KEY.format(ip=ip)):
            raise exceptions.Throttled(
                wait=settings.AUTH_FAILURE_WINDOW_SECONDS, detail="Too many invalid sign-in tokens."
            )
        try:
            return self._authenticate_token(request, header[1], ip)
        except exceptions.AuthenticationFailed:
            _record_failure(ip)
            raise

    def _authenticate_token(self, request, raw_token: bytes, ip: str):
        if len(raw_token) > MAX_TOKEN_BYTES:
            raise exceptions.AuthenticationFailed("Invalid authorization header.")
        try:
            token = raw_token.decode("ascii")
        except UnicodeDecodeError:
            raise exceptions.AuthenticationFailed("Invalid authorization header.") from None

        claims: dict[str, Any] | None = None
        last_error: Exception | None = None
        verification_mode = settings.SUPABASE_JWT_VERIFICATION
        if verification_mode == "jwks" and settings.SUPABASE_URL:
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
        elif verification_mode in {"jwks", "hs256"} and settings.SUPABASE_JWT_SECRET:
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

        from apps.identity.sessions import SessionRevoked, ensure_session_active

        try:
            ensure_session_active(claims)
        except SessionRevoked as exc:
            raise exceptions.AuthenticationFailed(str(exc)) from None

        email = claims.get("email") if isinstance(claims.get("email"), str) else ""
        # Supabase owns the credentials (the bcrypt hash is in auth.users); the Django password is
        # explicitly unusable so the row cannot be used for a Django login.
        user, created = User.objects.get_or_create(
            supabase_user_id=subject, defaults={"email": email, "password": make_password(None)}
        )
        if not user.is_active:
            raise exceptions.AuthenticationFailed("This user account is disabled.")
        if not created and email and user.email != email:
            User.objects.filter(pk=user.pk).update(email=email)
            user.email = email
        from apps.identity.provisioning import provision_on_authentication

        django_request = getattr(request, "_request", request)
        provision_on_authentication(
            user,
            claims,
            created=created,
            ip=None if ip == "unknown" else ip,
            user_agent=django_request.META.get("HTTP_USER_AGENT", ""),
        )
        # Membership and role answers are memoized on this request's user object only
        # (apps.identity.authorization.request_scoped_cache); set after provisioning created any workspace.
        user._jt_authz_cache = {}
        return user, claims

    def authenticate_header(self, request) -> str:
        return self.keyword
