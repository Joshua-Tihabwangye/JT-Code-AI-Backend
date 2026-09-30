import time
import uuid

import jwt as pyjwt
import pytest
from django.urls import reverse
from rest_framework.test import APIClient

import apps.identity.authentication as auth_module
from apps.identity.authentication import SupabaseJWTAuthentication
from apps.identity.models import User


def test_authenticate_header():
    assert SupabaseJWTAuthentication().authenticate_header(None) == "Bearer"


def _make_token(payload_extra=None, secret="test-jwt-secret", alg="HS256"):
    payload = {
        "sub": "test-supabase-user-id",
        "email": "test@example.com",
        "aud": "authenticated",
        "role": "authenticated",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "jti": str(uuid.uuid4()),
    }
    if payload_extra:
        payload.update(payload_extra)
    return pyjwt.encode(payload, secret, algorithm=alg)


@pytest.mark.django_db
def test_authenticated_ping_endpoint(authenticated_client):
    response = authenticated_client.get(reverse("auth-ping"))
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["email"] == "test@example.com"


@pytest.mark.django_db
def test_unauthenticated_ping_rejected():
    client = APIClient()
    response = client.get(reverse("auth-ping"))
    assert response.status_code == 401


@pytest.mark.django_db
def test_valid_hs256_token_authenticates(api_client, user):
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {_make_token()}")
    response = api_client.get(reverse("auth-ping"))
    assert response.status_code == 200
    assert response.json()["supabaseUserId"] == user.supabase_user_id


@pytest.mark.django_db
def test_expired_token_rejected(api_client):
    token = _make_token({"exp": int(time.time()) - 60, "iat": int(time.time()) - 120})
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = api_client.get(reverse("auth-ping"))
    assert response.status_code == 401


@pytest.mark.django_db
def test_garbage_token_rejected(api_client):
    api_client.credentials(HTTP_AUTHORIZATION="Bearer not-a-jwt")
    response = api_client.get(reverse("auth-ping"))
    assert response.status_code == 401


@pytest.mark.django_db
def test_missing_bearer_header_rejected(api_client):
    response = api_client.get(reverse("auth-ping"))
    assert response.status_code == 401


@pytest.mark.django_db
def test_user_auto_created_from_valid_token(api_client):
    email = "newuser@example.com"
    api_client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {_make_token({'sub': 'brand-new-sub', 'email': email})}"
    )
    response = api_client.get(reverse("auth-ping"))
    assert response.status_code == 200
    assert User.objects.filter(supabase_user_id="brand-new-sub", email=email).exists()


@pytest.mark.django_db
def test_es256_jwks_token_authenticates(api_client, monkeypatch):
    """Verify tokens signed with an ES256 key pair found in a JWKS payload."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()

    def b64u(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    jwk = {
        "kid": "test-es256-key",
        "kty": "EC",
        "crv": "P-256",
        "x": b64u(public_key.public_numbers().x.to_bytes(32, "big")),
        "y": b64u(public_key.public_numbers().y.to_bytes(32, "big")),
    }

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    def fake_jwks():
        return {"keys": [jwk]}

    monkeypatch.setattr(auth_module, "_fetch_jwks", fake_jwks)
    monkeypatch.setattr(auth_module.settings, "SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setattr(auth_module.settings, "SUPABASE_JWT_SECRET", "")

    # The real Supabase project publishes its verify key as a JWK; PyJWT decodes
    # the raw JWK dict via PyJWK, exactly as authentication.py does.
    from jwt import PyJWK

    token = pyjwt.encode(
        {
            "sub": "es256-user",
            "email": "es256@example.com",
            "aud": "authenticated",
            "role": "authenticated",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        private_pem,
        algorithm="ES256",
        headers={"kid": "test-es256-key"},
    )
    assert PyJWK.from_dict(jwk).key is not None

    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = api_client.get(reverse("auth-ping"))
    assert response.status_code == 200
    assert response.json()["supabaseUserId"] == "es256-user"


@pytest.mark.django_db
def test_jwks_failure_does_not_fall_back_to_hmac(api_client, user, monkeypatch):
    monkeypatch.setattr(auth_module.settings, "SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setattr(auth_module, "_fetch_jwks", lambda: (_ for _ in ()).throw(ValueError("offline")))
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {_make_token()}")

    response = api_client.get(reverse("auth-ping"))

    assert response.status_code == 401


@pytest.mark.django_db
def test_disabled_user_is_not_reactivated_by_a_valid_token(api_client, user):
    user.is_active = False
    user.save(update_fields=["is_active"])
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {_make_token()}")

    response = api_client.get(reverse("auth-ping"))

    user.refresh_from_db()
    assert response.status_code == 401
    assert user.is_active is False
