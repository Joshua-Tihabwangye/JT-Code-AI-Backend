"""Secret comparisons reject, never crash on, arbitrary header text (backlog P9-1).

``hmac.compare_digest`` raises ``TypeError`` for ``str`` values with non-ASCII characters. Header values
are latin-1 text the client chooses, so every secret check goes through ``constant_time_equals``.
"""

from __future__ import annotations

import json
import time

import pytest
from django.test import RequestFactory, override_settings
from django.urls import reverse

from apps.core.edge import via_cloudflare
from apps.core.signing import (
    NONCE_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    SignatureError,
    compute_signature,
    constant_time_equals,
    verify_request,
)

NON_ASCII = "sécret-ÿ"  # what Django makes of header bytes 0xe9 / 0xff (latin-1)


@pytest.mark.parametrize(
    ("supplied", "expected", "equal"),
    [
        ("secret", "secret", True),
        (b"secret", "secret", True),
        ("secret", "other", False),
        (NON_ASCII, "secret", False),
        (NON_ASCII, NON_ASCII, True),
        ("", "", False),
        (None, "secret", False),
        ("secret", "", False),
    ],
)
def test_constant_time_equals(supplied, expected, equal):
    assert constant_time_equals(supplied, expected) is equal


@override_settings(CLOUDFLARE_ORIGIN_SECRET="origin-secret")
def test_cloudflare_origin_lock_rejects_non_ascii_header():
    from apps.core.edge import ORIGIN_AUTH_HEADER

    header = "HTTP_" + ORIGIN_AUTH_HEADER.upper().replace("-", "_")
    assert via_cloudflare(RequestFactory().get("/", **{header: NON_ASCII})) is False
    assert via_cloudflare(RequestFactory().get("/", **{header: "origin-secret"})) is True


def _signed_headers(secret: str, body: bytes, signature: str | None = None) -> dict[str, str]:
    timestamp, nonce = str(int(time.time())), "n" * 32
    value = signature if signature is not None else compute_signature(secret, timestamp, nonce, body)
    return {SIGNATURE_HEADER: f"v1={value}", TIMESTAMP_HEADER: timestamp, NONCE_HEADER: nonce}


def test_signed_request_with_non_ascii_signature_is_invalid():
    body = b"{}"
    with pytest.raises(SignatureError) as error:
        verify_request(
            body=body,
            headers=_signed_headers("k", body, signature=NON_ASCII),
            secrets_=["k"],
            namespace="test",
            tolerance_seconds=300,
        )
    assert error.value.code == "invalid"


@override_settings(METRICS_AUTH_TOKEN="metrics-token")
def test_metrics_token_rejects_non_ascii_bearer(client):
    assert client.get("/metrics", HTTP_AUTHORIZATION=f"Bearer {NON_ASCII}").status_code == 401


@override_settings(SUPABASE_WEBHOOK_SIGNING_SECRET="supabase-webhook-secret")
def test_supabase_webhook_rejects_non_ascii_bearer(client):
    response = client.post(
        reverse("supabase-webhook"),
        data=json.dumps({"type": "INSERT", "record": {}}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {NON_ASCII}",
    )
    assert response.status_code == 401
