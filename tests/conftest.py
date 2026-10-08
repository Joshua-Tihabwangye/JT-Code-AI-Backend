import os
import sys
from pathlib import Path

import pytest

# Add the project root to Python path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Configure Django settings before importing Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

TEST_WALLET_CREDITS = 1_000_000


@pytest.fixture(autouse=True)
def reset_rate_limits():
    """Rate-limit counters (per IP/user/tenant) must not leak between tests."""
    from django.core.cache import caches

    caches["rate_limits"].clear()


@pytest.fixture(autouse=True)
def fund_new_organizations(request):
    """Give every organization created in a test a funded wallet.

    Metering (Phase 13) reserves credits for chat, jobs, agent runs and other
    billable work. Tests of the insufficient-credit path opt out with
    ``@pytest.mark.unfunded``; metering tests set balances explicitly.
    """
    if request.node.get_closest_marker("unfunded"):
        yield
        return
    from decimal import Decimal

    from django.db.models.signals import post_save

    from apps.billing.models import CreditWallet
    from apps.identity.models import Organization

    def fund(sender, instance, created, **kwargs):
        if created:
            CreditWallet.objects.get_or_create(
                organization=instance, defaults={"balance": Decimal(TEST_WALLET_CREDITS)}
            )

    post_save.connect(fund, sender=Organization, dispatch_uid="test-fund-wallets")
    try:
        yield
    finally:
        post_save.disconnect(sender=Organization, dispatch_uid="test-fund-wallets")


@pytest.fixture(scope="session")
def celery_config():
    """Configure Celery for testing."""
    return {
        "broker_url": "memory://",
        "result_backend": "cache+memory://",
        "task_always_eager": True,
        "task_eager_propagates": True,
    }


@pytest.fixture
def api_client():
    """Provide a DRF API client for testing."""
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def authenticated_client(api_client, user):
    """Provide an authenticated API client using a Supabase-style JWT."""
    import time
    import uuid

    import jwt as pyjwt

    token = pyjwt.encode(
        {
            "sub": user.supabase_user_id,
            "email": user.email,
            "aud": "authenticated",
            "role": "authenticated",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "jti": str(uuid.uuid4()),
        },
        settings.SUPABASE_JWT_SECRET,
        algorithm="HS256",
    )
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return api_client


@pytest.fixture
def user(django_user_model):
    """Create a test user with a Supabase user id."""
    return django_user_model.objects.create_user(
        username="test-user",
        supabase_user_id="test-supabase-user-id",
        email="test@example.com",
        password="testpass123",
    )


@pytest.fixture
def admin_user(django_user_model):
    """Create an admin test user with a Supabase user id."""
    return django_user_model.objects.create_superuser(
        username="test-admin",
        supabase_user_id="test-admin-supabase-user-id",
        email="admin@example.com",
        password="adminpass123",
    )
