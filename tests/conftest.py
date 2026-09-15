import os
import sys
import pytest
from pathlib import Path

# Add the project root to Python path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Configure Django settings before importing Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")

import django
django.setup()

from django.conf import settings
from django.test.utils import get_runner


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
        password="testpass123"
    )


@pytest.fixture
def admin_user(django_user_model):
    """Create an admin test user with a Supabase user id."""
    return django_user_model.objects.create_superuser(
        username="test-admin",
        supabase_user_id="test-admin-supabase-user-id",
        email="admin@example.com",
        password="adminpass123"
    )
