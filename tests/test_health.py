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


def test_readiness_closes_the_broker_connection_it_opens(client, settings, monkeypatch):
    # Under uvicorn a dropped broker connection (socket + epoll) was never collected: Docker's probe
    # every 15 s leaked descriptors until the API worker hit its limit and answered 500 to requests.
    from unittest import mock

    from celery import current_app

    settings.HEALTHCHECK_EXTERNAL_DEPENDENCIES = True
    connection = mock.MagicMock()
    connection.__enter__.return_value = connection
    monkeypatch.setattr(current_app, "connection_for_read", lambda: connection)
    monkeypatch.setattr("confluent_kafka.admin.AdminClient", mock.MagicMock())

    response = client.get("/api/v1/health/ready/")

    assert response.json()["checks"]["celery_broker"] == "ok"
    connection.ensure_connection.assert_called_once_with(max_retries=0, timeout=3)
    connection.__exit__.assert_called_once()
