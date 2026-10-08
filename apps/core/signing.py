"""Timestamped, nonce-bound HMAC request signing with replay protection.

Used for every machine-to-machine webhook JT-Code owns (n8n in both
directions, the n8n Sentry relay, custom Supabase senders). The scheme::

    X-JT-Code-Timestamp: <unix seconds>
    X-JT-Code-Nonce:     <16-128 url-safe chars, unique per request>
    X-JT-Code-Signature: v1=<hex HMAC-SHA256(secret, "<timestamp>.<nonce>." + raw body)>

Verification rejects a missing/malformed header, a timestamp outside the
tolerance window, a signature that matches none of the configured secrets
(several may be configured during rotation; ``v1=`` may repeat), and - after
the signature is proven - a nonce already seen inside the window (``SET NX``
in the shared cache, so replays are rejected across processes).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from django.core.cache import cache

TIMESTAMP_HEADER = "X-JT-Code-Timestamp"
NONCE_HEADER = "X-JT-Code-Nonce"
SIGNATURE_HEADER = "X-JT-Code-Signature"
DEFAULT_TOLERANCE_SECONDS = 300
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


@dataclass
class SignatureError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def compute_signature(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = timestamp.encode() + b"." + nonce.encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign_request(
    body: bytes, secret: str, *, timestamp: int | None = None, nonce: str | None = None
) -> dict[str, str]:
    """Return the headers that authenticate ``body`` for the receiver."""
    if not secret:
        raise ValueError("A signing secret is required.")
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    nonce = nonce or secrets.token_urlsafe(24)
    return {
        TIMESTAMP_HEADER: stamp,
        NONCE_HEADER: nonce,
        SIGNATURE_HEADER: f"v1={compute_signature(secret, stamp, nonce, body)}",
    }


def _signatures(header: str) -> list[str]:
    values = []
    for part in header.split(","):
        scheme, _, value = part.strip().partition("=")
        if scheme == "v1" and value:
            values.append(value.strip())
    return values


def verify_request(
    *,
    body: bytes,
    headers: Mapping[str, str],
    secrets_: Iterable[str],
    namespace: str,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> str:
    """Authenticate a signed request and consume its nonce; return the nonce.

    Raises :class:`SignatureError` with a stable ``code``: ``not_configured``,
    ``missing``, ``stale``, ``invalid`` or ``replayed``.
    """
    keys = [key for key in secrets_ if key]
    if not keys:
        raise SignatureError("not_configured", "Request signing is not configured.")
    timestamp = headers.get(TIMESTAMP_HEADER, "")
    nonce = headers.get(NONCE_HEADER, "")
    supplied = _signatures(headers.get(SIGNATURE_HEADER, ""))
    if not timestamp.isdigit() or not _NONCE.fullmatch(nonce) or not supplied:
        raise SignatureError("missing", "Signature, timestamp and nonce headers are required.")
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > tolerance_seconds:
        raise SignatureError("stale", "Request timestamp is outside the allowed window.")
    expected = [compute_signature(key, timestamp, nonce, body) for key in keys]
    if not any(hmac.compare_digest(given, wanted) for given in supplied for wanted in expected):
        raise SignatureError("invalid", "Request signature is invalid.")
    if not cache.add(f"signed-nonce:{namespace}:{nonce}", "1", timeout=tolerance_seconds * 2):
        raise SignatureError("replayed", "Request was already received.")
    return nonce
