"""Encryption at rest for tool credentials (Fernet with key rotation).

``TOOL_CREDENTIALS_ENCRYPTION_KEYS`` is a comma-separated list of Fernet keys;
the first encrypts, all decrypt, so keys rotate without downtime. Development
may derive a key from ``SECRET_KEY``; staging/production must set it explicitly.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings


class CredentialDecryptionError(RuntimeError):
    """A stored credential could not be decrypted with any configured key."""


def _keys() -> list[bytes]:
    configured = [key.strip() for key in settings.TOOL_CREDENTIALS_ENCRYPTION_KEYS if key.strip()]
    if configured:
        return [key.encode() for key in configured]
    derived = hashlib.sha256(f"jt-code-tool-credentials:{settings.SECRET_KEY}".encode()).digest()
    return [base64.urlsafe_b64encode(derived)]


def _cipher() -> MultiFernet:
    return MultiFernet([Fernet(key) for key in _keys()])


def encrypt_secret(plaintext: str) -> str:
    return _cipher().encrypt(plaintext.encode()).decode() if plaintext else ""


def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    try:
        return _cipher().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise CredentialDecryptionError(
            "Stored credential cannot be decrypted with the configured keys."
        ) from exc


def rotate_secret(token: str) -> str:
    """Re-encrypt ``token`` under the primary key (used after adding a new key)."""
    return _cipher().rotate(token.encode()).decode() if token else ""
