"""Validation and signing helpers for outbound job callbacks."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import time
from urllib.parse import urlsplit

from django.conf import settings
from rest_framework.exceptions import ValidationError


def _host_matches(host: str, allowed_host: str) -> bool:
    normalized = allowed_host.lower().rstrip(".")
    if normalized.startswith("*."):
        suffix = normalized[1:]
        return host.endswith(suffix) and host != suffix[1:]
    return hmac.compare_digest(host, normalized)


def validate_callback_url(value: str) -> str:
    """Accept only HTTPS URLs at an explicitly trusted callback host."""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.fragment
        or (parsed.port not in (None, 443))
    ):
        raise ValidationError("callback_url must be an HTTPS URL without credentials or a fragment.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValidationError("callback_url must use an approved DNS hostname, not an IP address.")
    allowed_hosts = tuple(getattr(settings, "WEBHOOK_ALLOWED_HOSTS", ()))
    if not allowed_hosts or not any(_host_matches(host, item) for item in allowed_hosts):
        raise ValidationError("callback_url host is not approved for outbound callbacks.")
    return value


def signed_callback_request(callback) -> tuple[bytes, dict[str, str]]:
    """Serialize a stable payload and return authenticated delivery headers."""
    secret = getattr(settings, "WEBHOOK_SIGNING_SECRET", "")
    if not secret:
        raise RuntimeError("Outbound callback signing is not configured.")
    body = json.dumps(
        callback.payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    timestamp = str(int(time.time()))
    signed_value = b".".join((str(callback.id).encode(), timestamp.encode(), body))
    signature = hmac.new(secret.encode("utf-8"), signed_value, hashlib.sha256).hexdigest()
    return body, {
        "Content-Type": "application/json",
        "Idempotency-Key": str(callback.id),
        "User-Agent": "JT-Code-Callback/1.0",
        "X-JT-Code-Callback-Id": str(callback.id),
        "X-JT-Code-Timestamp": timestamp,
        "X-JT-Code-Signature": f"sha256={signature}",
    }
