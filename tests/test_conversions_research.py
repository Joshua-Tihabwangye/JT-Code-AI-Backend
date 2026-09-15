import io

import pytest
from django.core.cache import cache

from apps.conversions.models import ConversionJob
from apps.identity.models import Organization


@pytest.fixture
def org(user):
    org = Organization.objects.create(name='Conv Org', owner=user)
    user.organizations.add(org)
    return org


@pytest.fixture
def credit_balance(user, org):
    from apps.billing.services import CreditService

    wallet = CreditService.get_or_create_wallet(org)
    CreditService.add_credits(wallet, 10000, reason='Test credits')
    return wallet


def _upload_image(upload_format: str = 'png') -> io.BytesIO:
    from PIL import Image

    buffer = io.BytesIO()
    image = Image.new('RGB', (8, 8), (200, 120, 40))
    image.save(buffer, format=upload_format.upper())
    buffer.seek(0)
    buffer.name = f'test.{upload_format}'
    return buffer


# --- Conversions ---

@pytest.mark.django_db
def test_conversion_markdown_to_pdf(authenticated_client, user, org, credit_balance):
    response = authenticated_client.post('/api/v1/conversions/', {
        'input_format': 'md',
        'output_format': 'pdf',
        'content': '# Title\n\nHello **world**',
    })
    assert response.status_code == 201, response.content
    body = response.json()
    assert body['status'] == 'completed'
    assert body['output_format'] == 'pdf'
    assert body['output_bytes'] > 0
    job = ConversionJob.objects.get(id=body['id'])
    assert job.status == ConversionJob.Status.COMPLETED
    assert '/conversions/' in body['output_url'] or job.output_path


@pytest.mark.django_db
def test_conversion_markdown_to_docx(authenticated_client, user, org, credit_balance):
    response = authenticated_client.post('/api/v1/conversions/', {
        'input_format': 'md',
        'output_format': 'docx',
        'content': '# Title\n\n- item one\n- item two',
    })
    assert response.status_code == 201, response.content
    body = response.json()
    assert body['status'] == 'completed'
    assert body['output_format'] == 'docx'
    assert body['output_bytes'] > 0


@pytest.mark.django_db
def test_conversion_pdf_to_text(authenticated_client, user, org, credit_balance):
    from weasyprint import HTML

    pdf_bytes = HTML(string='<h1>PDF page</h1><p>Extractable text</p>').write_pdf()
    buffer = io.BytesIO(pdf_bytes)
    buffer.name = 'sample.pdf'
    response = authenticated_client.post('/api/v1/conversions/', {
        'input_format': 'pdf',
        'output_format': 'txt',
        'file': buffer,
    }, format='multipart')
    assert response.status_code == 201, response.content
    body = response.json()
    assert body['status'] == 'completed'
    assert body['output_format'] == 'txt'
    assert body['output_bytes'] > 0


@pytest.mark.django_db
def test_conversion_image_format(authenticated_client, user, org, credit_balance):
    response = authenticated_client.post('/api/v1/conversions/', {
        'input_format': 'png',
        'output_format': 'jpg',
        'file': _upload_image('png'),
    }, format='multipart')
    assert response.status_code == 201, response.content
    body = response.json()
    assert body['status'] == 'completed'
    assert body['output_format'] == 'jpg'
    assert body['output_bytes'] > 0


@pytest.mark.django_db
def test_conversion_disallowed_matrix(authenticated_client, user, org, credit_balance):
    response = authenticated_client.post('/api/v1/conversions/', {
        'input_format': 'docx',
        'output_format': 'docx',
        'content': 'nope',
    })
    assert response.status_code == 400


@pytest.mark.django_db
def test_conversion_requires_input(authenticated_client, user, org, credit_balance):
    response = authenticated_client.post('/api/v1/conversions/', {
        'input_format': 'md',
        'output_format': 'pdf',
    })
    assert response.status_code == 400


@pytest.mark.django_db
def test_conversion_insufficient_credits(authenticated_client, user, org):
    response = authenticated_client.post('/api/v1/conversions/', {
        'input_format': 'md',
        'output_format': 'pdf',
        'content': '# Hi',
    })
    assert response.status_code == 402


@pytest.mark.django_db
def test_conversion_download_local_file(authenticated_client, user, org, credit_balance):
    create = authenticated_client.post('/api/v1/conversions/', {
        'input_format': 'md',
        'output_format': 'pdf',
        'content': '# Download me\n\nBody text',
    })
    assert create.status_code == 201, create.content
    job = ConversionJob.objects.get(id=create.json()['id'])
    download = authenticated_client.get(f'/api/v1/conversions/{job.id}/download/')
    assert download.status_code == 200, download.content
    assert download['Content-Type'].startswith('application/pdf')


@pytest.mark.django_db
def test_conversion_download_denies_other_user(
    authenticated_client, user, org, credit_balance, django_user_model
):
    create = authenticated_client.post('/api/v1/conversions/', {
        'input_format': 'md',
        'output_format': 'pdf',
        'content': '# Private',
    })
    assert create.status_code == 201, create.content
    job_id = create.json()['id']

    other = django_user_model.objects.create_user(
        username='conv-other', supabase_user_id='conv-other-id', email='other-conv@example.com'
    )
    other_org = Organization.objects.create(name='Other Org', owner=other)
    other.organizations.add(other_org)
    import time
    import uuid

    import jwt as pyjwt
    from django.conf import settings
    from rest_framework.test import APIClient

    token = pyjwt.encode(
        {
            'sub': other.supabase_user_id, 'email': other.email, 'aud': 'authenticated',
            'role': 'authenticated', 'iat': int(time.time()), 'exp': int(time.time()) + 3600,
            'jti': str(uuid.uuid4()),
        },
        settings.SUPABASE_JWT_SECRET,
        algorithm='HS256',
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    response = client.get(f'/api/v1/conversions/{job_id}/download/')
    assert response.status_code == 404


@pytest.mark.django_db
def test_conversion_ownership_scoped(authenticated_client, user, org, credit_balance, django_user_model):
    create = authenticated_client.post('/api/v1/conversions/', {
        'input_format': 'md',
        'output_format': 'pdf',
        'content': '# Mine',
    })
    assert create.status_code == 201, create.content
    job_id = create.json()['id']

    other = django_user_model.objects.create_user(
        username='conv-owner-other', supabase_user_id='conv-owner-other-id',
        email='other-owner@example.com'
    )
    other_org = Organization.objects.create(name='Owner Other', owner=other)
    other.organizations.add(other_org)
    import time
    import uuid

    import jwt as pyjwt
    from django.conf import settings
    from rest_framework.test import APIClient

    token = pyjwt.encode(
        {
            'sub': other.supabase_user_id, 'email': other.email, 'aud': 'authenticated',
            'role': 'authenticated', 'iat': int(time.time()), 'exp': int(time.time()) + 3600,
            'jti': str(uuid.uuid4()),
        },
        settings.SUPABASE_JWT_SECRET,
        algorithm='HS256',
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    response = client.get(f'/api/v1/conversions/{job_id}/')
    assert response.status_code == 404


# --- Research jobs ---

@pytest.mark.django_db
def test_research_job_created(authenticated_client, user, org, credit_balance):
    response = authenticated_client.post('/api/v1/research/jobs/', {
        'query': 'What is the state of AI regulation in 2026?',
        'depth': 'standard',
    })
    assert response.status_code == 202, response.content
    body = response.json()
    assert body['status'] == 'queued'
    assert body['estimated_credits'] == '75'
    assert 'job_id' in body
    from apps.jobs.models import Job
    job = Job.objects.get(id=body['job_id'])
    assert job.task_type == Job.TaskType.SEARCH_RESEARCH
    assert job.input_payload['query'].startswith('What is')


@pytest.mark.django_db
def test_research_job_deep_cost(authenticated_client, user, org, credit_balance):
    response = authenticated_client.post('/api/v1/research/jobs/', {
        'query': 'Deep research question',
        'depth': 'deep',
    })
    assert response.status_code == 202, response.content
    assert response.json()['estimated_credits'] == '150'


@pytest.mark.django_db
def test_research_job_requires_query(authenticated_client, user, org, credit_balance):
    response = authenticated_client.post('/api/v1/research/jobs/', {'depth': 'standard'})
    assert response.status_code == 400


@pytest.mark.django_db
def test_research_job_rejects_bad_depth(authenticated_client, user, org, credit_balance):
    response = authenticated_client.post('/api/v1/research/jobs/', {
        'query': 'Question?', 'depth': 'ultra',
    })
    assert response.status_code == 400


@pytest.mark.django_db
def test_research_job_insufficient_credits(authenticated_client, user, org):
    response = authenticated_client.post('/api/v1/research/jobs/', {
        'query': 'Question without credits',
    })
    assert response.status_code == 402


# --- Regression: settings/profile serialization ---

@pytest.mark.django_db
def test_settings_profile_serializes_avatar_url(authenticated_client, user):
    response = authenticated_client.get('/api/v1/settings/profile/')
    assert response.status_code == 200, response.content
    assert 'avatar_url' in response.json()


# --- Regression: job idempotency key uniqueness ---

@pytest.mark.django_db
def test_repeated_research_jobs_get_unique_idempotency_keys(
    authenticated_client, user, org, credit_balance
):
    payload = {'query': 'Repeated identical research question?', 'depth': 'standard'}
    first = authenticated_client.post('/api/v1/research/jobs/', payload)
    assert first.status_code == 202, first.content
    second = authenticated_client.post('/api/v1/research/jobs/', payload)
    assert second.status_code == 202, second.content
    assert first.json()['job_id'] != second.json()['job_id']
    from apps.jobs.models import Job
    jobs = Job.objects.filter(owner=user, task_type=Job.TaskType.SEARCH_RESEARCH)
    assert len(set(job.idempotency_key for job in jobs)) == 2


# --- Rate limits ---

@pytest.mark.django_db
def test_chat_throttle_blocks_burst(authenticated_client, user, org):
    from apps.conversations.models import Conversation

    conversation = Conversation.objects.create(owner=user, title='Throttle test')
    cache.clear()
    responses = []
    for _ in range(31):
        response = authenticated_client.post('/api/v1/chat/requests/', {
            'conversationId': str(conversation.id),
            'chatInput': 'ping',
        }, HTTP_IDEMPOTENCY_KEY=f'key-{_}')
        responses.append(response.status_code)
    assert 429 in responses, responses
    assert responses[-1] == 429


@pytest.mark.django_db
def test_embedding_throttle(authenticated_client, user, org):
    cache.clear()
    responses = []
    for _ in range(121):
        response = authenticated_client.post('/api/v1/embeddings/', {'texts': ['embed me']})
        responses.append(response.status_code)
    assert 429 in responses
