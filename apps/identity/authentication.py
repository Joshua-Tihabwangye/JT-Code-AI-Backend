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


def _fetch_jwks() -> dict[str, Any]:
    global _JWKS_CACHE, _JWKS_FETCHED_AT
    now = time.monotonic()
    if _JWKS_CACHE is not None and now - _JWKS_FETCHED_AT < _JWKS_TTL_SECONDS:
        return _JWKS_CACHE

    jwks_url = settings.SUPABASE_URL.rstrip('/') + '/auth/v1/.well-known/jwks.json'
    with httpx.Client(timeout=10.0) as client:
        response = client.get(jwks_url)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get('keys'), list):
        raise ValueError('Invalid JWKS payload from Supabase.')
    _JWKS_CACHE = payload
    _JWKS_FETCHED_AT = now
    return payload


def _verify_with_jwks(token: str) -> dict[str, Any]:
    from jwt import PyJWK

    jwks = _fetch_jwks()
    unverified_header = jwt.get_unverified_header(token)
    kid = unverified_header.get('kid')
    key = None
    if kid:
        key = next((k for k in jwks['keys'] if k.get('kid') == kid), None)
    if key is None and jwks['keys']:
        key = jwks['keys'][0]
    if key is None:
        raise ValueError('No matching JWKS key for token.')

    return jwt.decode(
        token,
        PyJWK.from_dict(key).key,
        algorithms=['ES256', 'RS256', 'HS256'],
        audience=settings.SUPABASE_JWT_AUDIENCE or 'authenticated',
        issuer=settings.SUPABASE_JWT_ISSUER or None,
        options={'verify_aud': bool(settings.SUPABASE_JWT_AUDIENCE)},
        leeway=30,
    )


def _verify_with_secret(token: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        settings.SUPABASE_JWT_SECRET,
        algorithms=['HS256'],
        audience=settings.SUPABASE_JWT_AUDIENCE or None,
        options={'verify_aud': bool(settings.SUPABASE_JWT_AUDIENCE)},
        leeway=30,
    )


class SupabaseJWTAuthentication(authentication.BaseAuthentication):
    keyword = 'Bearer'

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header:
            return None
        if len(header) != 2 or header[0].decode().lower() != self.keyword.lower():
            raise exceptions.AuthenticationFailed('Invalid authorization header.')
        token = header[1].decode()

        claims: dict[str, Any] | None = None
        last_error: Exception | None = None
        if settings.SUPABASE_URL:
            try:
                claims = _verify_with_jwks(token)
            except jwt.ExpiredSignatureError:
                raise exceptions.AuthenticationFailed('Supabase token has expired.') from None
            except Exception as exc:
                last_error = exc
        if claims is None and settings.SUPABASE_JWT_SECRET:
            try:
                claims = _verify_with_secret(token)
            except jwt.ExpiredSignatureError:
                raise exceptions.AuthenticationFailed('Supabase token has expired.') from None
            except Exception as exc:
                last_error = exc
        if claims is None:
            raise exceptions.AuthenticationFailed(
                'Invalid or expired Supabase session token.'
            ) from last_error

        subject = claims.get('sub')
        if not subject:
            raise exceptions.AuthenticationFailed('Supabase token is missing subject.')

        defaults: dict[str, Any] = {'is_active': True}
        if isinstance(claims.get('email'), str):
            defaults['email'] = claims['email']
        if isinstance(claims.get('user_metadata'), dict):
            metadata = claims['user_metadata']
            if isinstance(metadata.get('full_name'), str):
                defaults['full_name'] = metadata['full_name']
            if isinstance(metadata.get('name'), str):
                defaults['display_name'] = metadata['name']
            if isinstance(metadata.get('avatar_url'), str):
                defaults['avatar_url'] = metadata['avatar_url']

        user, _ = User.objects.update_or_create(supabase_user_id=subject, defaults=defaults)
        return user, claims

    def authenticate_header(self, request) -> str:
        return self.keyword