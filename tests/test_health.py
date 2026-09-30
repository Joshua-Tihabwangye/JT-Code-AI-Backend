import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_live_endpoint(client):
    response = client.get(reverse("health-live"))
    assert response.status_code == 200
    assert response.json()["service"] == "jt-code-api"


@pytest.mark.django_db
def test_startup_and_readiness_endpoints_are_public_and_safe(client):
    startup = client.get(reverse("health-startup"))
    ready = client.get(reverse("health-ready"))

    assert startup.status_code == 200
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}
