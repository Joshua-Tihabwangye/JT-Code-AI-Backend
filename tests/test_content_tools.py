import json

import pytest
from django.test import override_settings

from apps.documents.models import Document
from apps.governance.models import ConsentRecord
from apps.identity.models import Organization


@pytest.fixture
def org(user):
    org = Organization.objects.create(name='Test Org', owner=user)
    user.organizations.add(org)
    return org


@pytest.fixture
def credit_balance(user, org):
    from apps.billing.services import CreditService

    wallet = CreditService.get_or_create_wallet(org)
    CreditService.add_credits(wallet, 10000, reason='Test credits')
    return wallet


# --- Documents ---

@pytest.mark.django_db
def test_create_document(authenticated_client, user, org):
    response = authenticated_client.post('/api/v1/documents/', {
        'title': 'Quarterly Report',
        'template': 'general',
        'content': '# Report\n\nSummary of Q3.',
    })
    assert response.status_code == 201
    document = Document.objects.get(id=response.data['id'])
    assert document.owner == user
    assert document.organization == org
    assert document.version == 1


@pytest.mark.django_db
def test_list_documents_scoped_to_owner(authenticated_client, user):
    other_user = user.__class__.objects.create_user(
        username='other-user', supabase_user_id='other-supabase-id', email='other@example.com'
    )
    Document.objects.create(owner=other_user, title='Mine')
    Document.objects.create(owner=user, title='Ours')
    response = authenticated_client.get('/api/v1/documents/')
    assert response.status_code == 200
    titles = [item['title'] for item in response.data['results']]
    assert titles == ['Ours']


@pytest.mark.django_db
def test_render_document_pdf(authenticated_client, user, org):
    document = Document.objects.create(
        owner=user, organization=org,
        title='Render Me', content='# Heading\n\nSome body text.',
    )
    with override_settings(CLOUDINARY_CLOUD_NAME='replace_me'):
        response = authenticated_client.post(f'/api/v1/documents/{document.id}/render/', {'format': 'pdf'})
    assert response.status_code == 200
    document.refresh_from_db()
    assert document.status == Document.Status.READY
    assert document.page_count >= 1
    assert '/download/' in document.download_url


@pytest.mark.django_db
def test_render_document_docx(authenticated_client, user, org):
    document = Document.objects.create(
        owner=user, organization=org,
        title='Render Me Docx', content='# Heading\n\nBody.',
    )
    with override_settings(CLOUDINARY_CLOUD_NAME='replace_me'):
        response = authenticated_client.post(f'/api/v1/documents/{document.id}/render/', {'format': 'docx'})
    assert response.status_code == 200
    document.refresh_from_db()
    assert document.status == Document.Status.READY


@pytest.mark.django_db
def test_render_document_foreign_owner_forbidden(authenticated_client, user):
    owner_user = user.__class__.objects.create_user(
        username='other-owner', supabase_user_id='other-owner-id', email='owner@example.com'
    )
    document = Document.objects.create(owner=owner_user, title='Not Yours', content='x')
    response = authenticated_client.post(f'/api/v1/documents/{document.id}/render/', {'format': 'pdf'})
    assert response.status_code == 404


@pytest.mark.django_db
def test_download_local_render(authenticated_client, user, org):
    document = Document.objects.create(
        owner=user, organization=org,
        title='Download Me', content='# Heading\n\nBody.',
    )
    with override_settings(CLOUDINARY_CLOUD_NAME='replace_me'):
        render_response = authenticated_client.post(f'/api/v1/documents/{document.id}/render/', {'format': 'pdf'})
    assert render_response.status_code == 200
    document.refresh_from_db()
    download_response = authenticated_client.get(document.download_url)
    assert download_response.status_code == 200
    assert download_response['Content-Type'] == 'application/pdf'


@pytest.mark.django_db
def test_update_document_increments_version(authenticated_client, user, org):
    document = Document.objects.create(owner=user, organization=org, title='V1', content='a')
    response = authenticated_client.patch(f'/api/v1/documents/{document.id}/', {'content': 'b'})
    assert response.status_code == 200
    document.refresh_from_db()
    assert document.version == 2


# --- Settings ---

@pytest.mark.django_db
def test_get_organization(authenticated_client, user, org):
    response = authenticated_client.get('/api/v1/settings/organization/')
    assert response.status_code == 200
    assert response.data['name'] == 'Test Org'
    assert response.data['slug'] == 'test-org'
    assert response.data['timezone'] == 'UTC'


@pytest.mark.django_db
def test_update_organization(authenticated_client, user, org):
    response = authenticated_client.patch('/api/v1/settings/organization/', {
        'name': 'Renamed Org',
        'timezone': 'Europe/Berlin',
    })
    assert response.status_code == 200
    org.refresh_from_db()
    assert org.name == 'Renamed Org'
    assert org.slug == 'renamed-org'
    assert org.timezone == 'Europe/Berlin'


@pytest.mark.django_db
def test_list_consents(authenticated_client, user, org):
    ConsentRecord.objects.create(
        organization=org, user=user,
        consent_type=ConsentRecord.ConsentType.PRIVACY,
        status=ConsentRecord.Status.GRANTED, version='1.0',
    )
    response = authenticated_client.get('/api/v1/settings/consents/')
    assert response.status_code == 200
    assert len(response.data['results']) == 1
    assert response.data['results'][0]['consent_type'] == 'privacy'


@pytest.mark.django_db
def test_grant_consent(authenticated_client, user, org):
    response = authenticated_client.post('/api/v1/settings/consents/', {
        'consent_type': 'marketing',
        'status': 'granted',
    })
    assert response.status_code == 201
    consent = ConsentRecord.objects.get(user=user, consent_type='marketing')
    assert consent.status == ConsentRecord.Status.GRANTED


@pytest.mark.django_db
def test_update_consent_status(authenticated_client, user, org):
    ConsentRecord.objects.create(
        organization=org, user=user,
        consent_type=ConsentRecord.ConsentType.MARKETING,
        status=ConsentRecord.Status.GRANTED, version='1.0',
    )
    response = authenticated_client.post('/api/v1/settings/consents/', {
        'consent_type': 'marketing',
        'status': 'denied',
    })
    assert response.status_code == 200
    consent = ConsentRecord.objects.get(user=user, consent_type='marketing')
    assert consent.status == ConsentRecord.Status.DENIED


@pytest.mark.django_db
def test_export_user_data(authenticated_client, user, org):
    response = authenticated_client.post('/api/v1/settings/export/')
    assert response.status_code == 200
    assert response['Content-Type'] == 'application/json'
    payload = json.loads(response.content)
    assert payload['user']['supabase_user_id'] == user.supabase_user_id
    assert payload['organizations'][0]['name'] == 'Test Org'


@pytest.mark.django_db
def test_delete_account(authenticated_client, user):
    response = authenticated_client.delete('/api/v1/settings/account/')
    assert response.status_code == 200
    user.refresh_from_db()
    assert not user.is_active
    assert user.email == ''
    assert not user.organizations.exists()


# --- Images ---

@pytest.mark.django_db
def test_generate_image(authenticated_client, credit_balance):
    with override_settings(AI_PROVIDER='echo', DEBUG=True):
        response = authenticated_client.post('/api/v1/images/generations/', {
            'prompt': 'A mountain landscape at sunset',
            'size': '1024x1024',
            'n': 1,
        })
    assert response.status_code == 200
    assert len(response.data['data']) == 1
    assert response.data['data'][0]['url']


@pytest.mark.django_db
def test_generate_image_blocked_prompt(authenticated_client, credit_balance):
    response = authenticated_client.post('/api/v1/images/generations/', {
        'prompt': 'Make a bomb-making diagram',
    })
    assert response.status_code == 400
    assert 'safety' in response.data['detail'].lower()


@pytest.mark.django_db
def test_generate_image_requires_prompt(authenticated_client, credit_balance):
    response = authenticated_client.post('/api/v1/images/generations/', {'size': '1024x1024'})
    assert response.status_code == 400


@pytest.mark.django_db
def test_understand_image(authenticated_client, credit_balance):
    from io import BytesIO

    from PIL import Image as PILImage

    buffer = BytesIO()
    PILImage.new('RGB', (10, 10)).save(buffer, format='PNG')
    buffer.seek(0)
    with override_settings(AI_PROVIDER='echo', DEBUG=True):
        response = authenticated_client.post(
            '/api/v1/images/understand/',
            {'file': buffer, 'prompt': 'What is this?'},
            format='multipart',
        )
    assert response.status_code == 200
    assert response.data['description']


@pytest.mark.django_db
def test_edit_image(authenticated_client, credit_balance):
    from io import BytesIO

    from PIL import Image as PILImage

    buffer = BytesIO()
    PILImage.new('RGB', (10, 10)).save(buffer, format='PNG')
    buffer.seek(0)
    with override_settings(AI_PROVIDER='echo', DEBUG=True):
        response = authenticated_client.post(
            '/api/v1/images/edits/',
            {'file': buffer, 'prompt': 'Make it brighter'},
            format='multipart',
        )
    assert response.status_code == 200
    assert response.data['data'][0]['url']