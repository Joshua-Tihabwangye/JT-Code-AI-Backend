import pytest
from django.urls import reverse

@pytest.mark.django_db
def test_live_endpoint(client):
    response = client.get(reverse('health-live'))
    assert response.status_code == 200
    assert response.json()['service'] == 'jt-code-api'
