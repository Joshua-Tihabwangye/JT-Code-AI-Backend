"""Supabase session revocation for stateless access tokens (backlog P2-1).

A Supabase access token stays cryptographically valid until ``exp`` (``JWT_EXP``, 3600 s), even after
the user signs out, is banned or is deleted. With ``SUPABASE_SESSION_CHECK=true`` Django also requires the
token's ``session_id`` to still exist in ``auth.sessions`` for that user, unexpired, for a user who is
neither banned nor deleted. GoTrue deletes the session row on sign-out (``scope=global`` removes all of
them), so revocation takes effect within ``SUPABASE_SESSION_CHECK_TTL_SECONDS``.

The database is the one Django already uses: self-hosted Supabase keeps ``auth`` next to ``public``.
Only positive results are cached; a revoked or unknown session is re-checked on every request.
The check fails closed: a token without ``session_id`` or a database error rejects the request.
"""

from __future__ import annotations

import logging
import uuid

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, connection

logger = logging.getLogger(__name__)

_CACHE_KEY = "jt-code:identity:session-ok:{session_id}:{user_id}"
_SQL = """
SELECT 1
FROM auth.sessions s
JOIN auth.users u ON u.id = s.user_id
WHERE s.id = %s AND s.user_id = %s
  AND (s.not_after IS NULL OR s.not_after > now())
  AND u.deleted_at IS NULL
  AND (u.banned_until IS NULL OR u.banned_until <= now())
LIMIT 1
"""


class SessionRevoked(Exception):
    """The token's Supabase session no longer exists, or its user is banned or deleted."""


def _uuid(value: object) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError, TypeError:
        return None


def session_is_active(session_id: str, user_id: str) -> bool:
    """True when ``auth.sessions`` still holds this session for an active user."""
    with connection.cursor() as cursor:
        cursor.execute(_SQL, [session_id, user_id])
        return cursor.fetchone() is not None


def ensure_session_active(claims: dict) -> None:
    """Raise :class:`SessionRevoked` unless the token's session is live (no-op when the check is off)."""
    if not settings.SUPABASE_SESSION_CHECK:
        return
    session_id, user_id = _uuid(claims.get("session_id")), _uuid(claims.get("sub"))
    if session_id is None or user_id is None:
        raise SessionRevoked("The token carries no Supabase session.")
    key = _CACHE_KEY.format(session_id=session_id, user_id=user_id)
    if cache.get(key):
        return
    try:
        active = session_is_active(session_id, user_id)
    except DatabaseError as exc:
        logger.warning("Supabase session check failed; rejecting the request", exc_info=True)
        raise SessionRevoked("The session could not be verified.") from exc
    if not active:
        raise SessionRevoked("This session has ended. Sign in again.")
    cache.set(key, True, settings.SUPABASE_SESSION_CHECK_TTL_SECONDS)
