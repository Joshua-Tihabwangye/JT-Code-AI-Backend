"""Tests for the AI gateway execution layer and the internal job executor."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.test import override_settings

from apps.ai_gateway.adapters import (
    AIGatewayError,
    AIProviderNotConfigured,
    ChatMessage,
    EchoChatAdapter,
    Usage,
    build_chat_adapter,
    get_adapter_for_provider,
)
from apps.ai_gateway.models import Model, ModelRun
from apps.ai_gateway.service import (
    ModelSelectionError,
    estimate_cost_usd,
    generate_completion,
    select_model,
)
from apps.events.models import OutboxEvent
from apps.identity.models import Organization
from apps.jobs.executor import execute_job
from apps.jobs.models import Job, JobStep


@pytest.fixture
def org(user):
    org = Organization.objects.create(name="AI Org", owner=user)
    user.organizations.add(org)
    return org


@pytest.fixture
def credit_balance(user, org):
    from apps.billing.services import CreditService

    wallet = CreditService.get_or_create_wallet(org)
    CreditService.add_credits(wallet, 10000, reason="Test credits")
    return wallet


def _fake_model(name="echo-chat"):
    return SimpleNamespace(name=name)


def _make_job(user, org, task_type, payload, **kwargs):
    return Job.objects.create(
        owner=user,
        organization=org,
        task_type=task_type,
        trace_id="t-1",
        input_payload=payload,
        **kwargs,
    )


# --- Adapters ---


def test_get_adapter_for_provider_map():
    assert get_adapter_for_provider("echo") is EchoChatAdapter
    assert get_adapter_for_provider("openai") is not None
    assert get_adapter_for_provider("google") is not None
    assert get_adapter_for_provider("anthropic") is None


def test_build_chat_adapter_unknown_provider():
    with pytest.raises(AIGatewayError):
        build_chat_adapter("anthropic")


@override_settings(AI_PROVIDER="echo")
def test_echo_adapter_deterministic_output():
    adapter = EchoChatAdapter()
    result = adapter.generate(
        messages=[ChatMessage("user", "hello there"), ChatMessage("assistant", "hi")],
        model=_fake_model("echo-chat"),
    )
    assert result.content.startswith("JT-Code development response")
    assert "hello there" in result.content
    assert result.model_name == "echo-chat"
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0


@override_settings(AI_PROVIDER="disabled")
def test_echo_adapter_requires_echo_mode():
    with pytest.raises(AIProviderNotConfigured):
        EchoChatAdapter().generate(messages=[ChatMessage("user", "hi")], model=_fake_model())


# --- Model selection ---


@pytest.mark.django_db
def test_select_model_uses_default_policy():
    model, policy = select_model(task_type="GENERAL_QUESTION")
    assert policy is not None
    assert policy.is_default
    assert policy.slug == "general-question"
    assert model.status == Model.Status.ACTIVE


@pytest.mark.django_db
def test_select_model_missing_policy_raises():
    with pytest.raises(ModelSelectionError) as exc_info:
        select_model(task_type="SCHEDULED_AUTOMATION")
    assert exc_info.value.code == "MODEL_POLICY_NOT_FOUND"


# --- Cost estimation ---


@pytest.mark.django_db
def test_estimate_cost_usd():
    model = Model.objects.get(name="echo-chat")
    cost = estimate_cost_usd(model, Usage(input_tokens=1000, output_tokens=500))
    assert cost >= Decimal("0")
    assert isinstance(cost, Decimal)


# --- generate_completion ---


@pytest.mark.django_db
@override_settings(AI_PROVIDER="echo")
def test_generate_completion_falls_back_to_echo_and_records_run():
    outcome = generate_completion(
        messages=[ChatMessage("user", "Hi JT-Code")],
        task_type="GENERAL_QUESTION",
        trace_id="run-1",
    )
    assert outcome.content.startswith("JT-Code development response")
    assert outcome.run.model.name == "echo-chat"
    assert outcome.run.status == ModelRun.Status.COMPLETED
    assert outcome.fallback_used is True
    # Fallbacks are per-attempt records; retry_count counts same-model retries only.
    attempts = outcome.run.metadata["attempts"]
    assert attempts[-1]["model"] == "echo-chat"
    assert all(a["error_code"] == "AI_PROVIDER_NOT_CONFIGURED" for a in attempts[:-1])
    assert outcome.run.retry_count == 0
    assert outcome.run.model_alias == "default-chat"
    assert outcome.run.input_tokens > 0 and outcome.run.output_tokens > 0
    assert outcome.run.provider_cost_usd >= Decimal("0")
    assert outcome.run.latency_ms is not None
    assert outcome.policy.slug == "general-question"


@pytest.mark.django_db
@override_settings(AI_PROVIDER="disabled")
def test_generate_completion_all_failures_records_failed_run():
    with pytest.raises(AIGatewayError) as exc_info:
        generate_completion(
            messages=[ChatMessage("user", "Hi")],
            task_type="GENERAL_QUESTION",
            trace_id="run-2",
        )
    assert exc_info.value.code == "AI_PROVIDER_NOT_CONFIGURED"
    run = ModelRun.objects.get(trace_id="run-2")
    assert run.status == ModelRun.Status.FAILED
    assert run.error_code == "AI_PROVIDER_NOT_CONFIGURED"


# --- Executor ---


@pytest.mark.django_db
@override_settings(AI_PROVIDER="echo")
def test_execute_general_question_job_completes(user, org, credit_balance):
    job = _make_job(
        user,
        org,
        Job.TaskType.GENERAL_QUESTION,
        {"messages": [{"role": "user", "content": "Hello"}]},
    )
    result = execute_job_task_delay(job)
    assert result["status"] == "completed"
    job.refresh_from_db()
    assert job.status == Job.Status.COMPLETED
    assert job.completed_at is not None
    assert "answer" in job.result
    assert job.result["usage"]["model"] == "echo-chat"
    step = JobStep.objects.get(job=job)
    assert step.status == JobStep.Status.COMPLETED
    assert step.input_tokens > 0
    run = ModelRun.objects.get(job_id=job.id)
    assert run.status == ModelRun.Status.COMPLETED
    assert run.model.name == "echo-chat"
    assert OutboxEvent.objects.filter(topic__endswith="jobs.job.completed").exists()


@pytest.mark.django_db
@override_settings(AI_PROVIDER="echo")
def test_execute_rag_query_job_completes_ungrounded(user, org, credit_balance):
    job = _make_job(
        user,
        org,
        Job.TaskType.RAG_QUERY,
        {"query": "What is the refund policy?"},
    )
    result = execute_job_task_delay(job)
    assert result["status"] == "completed"
    job.refresh_from_db()
    assert job.status == Job.Status.COMPLETED
    assert job.result["grounded"] is False
    assert job.result["sources"] == []
    assert "answer" in job.result


@pytest.mark.django_db
@override_settings(AI_PROVIDER="disabled")
def test_execute_general_question_job_fails(user, org, credit_balance):
    job = _make_job(
        user,
        org,
        Job.TaskType.GENERAL_QUESTION,
        {"messages": [{"role": "user", "content": "Hello"}]},
    )
    result = execute_job_task_delay(job)
    assert result["status"] == "failed"
    assert result["error_code"] == "AI_PROVIDER_NOT_CONFIGURED"
    job.refresh_from_db()
    assert job.status == Job.Status.FAILED
    assert job.error_code == "AI_PROVIDER_NOT_CONFIGURED"
    assert OutboxEvent.objects.filter(topic__endswith="jobs.job.failed").exists()


@pytest.mark.django_db
def test_execute_unsupported_task_type_fails_terminally(user, org):
    job = _make_job(
        user,
        org,
        Job.TaskType.IMAGE_GENERATION,
        {"prompt": "A cat"},
    )
    result = execute_job(job)
    assert result == {
        "status": "failed",
        "task_type": Job.TaskType.IMAGE_GENERATION,
        "error_code": "UNSUPPORTED_TASK_TYPE",
    }
    job.refresh_from_db()
    assert job.status == Job.Status.FAILED


# --- Completion API ---


@pytest.mark.django_db
@override_settings(AI_PROVIDER="echo")
def test_completion_api_accepts_and_completes_job(
    authenticated_client, user, org, credit_balance, django_capture_on_commit_callbacks
):
    # Jobs are dispatched after commit; run the eager worker as production would.
    with django_capture_on_commit_callbacks(execute=True):
        response = authenticated_client.post(
            "/api/v1/completion/",
            {
                "messages": [{"role": "user", "content": "Hello there"}],
                "task_type": "GENERAL_QUESTION",
            },
            format="json",
        )
    assert response.status_code == 202
    job = Job.objects.get(id=response.data["job_id"])
    assert job.status == Job.Status.COMPLETED
    assert job.result["answer"].startswith("JT-Code development response")
    assert ModelRun.objects.filter(job_id=job.id, status=ModelRun.Status.COMPLETED).exists()


@pytest.mark.django_db
def test_completion_api_requires_messages(authenticated_client, user, org, credit_balance):
    response = authenticated_client.post("/api/v1/completion/", {"task_type": "GENERAL_QUESTION"})
    assert response.status_code == 400


@pytest.mark.django_db
@override_settings(AI_PROVIDER="echo")
def test_completion_api_unknown_policy_404(authenticated_client, user, org, credit_balance):
    response = authenticated_client.post(
        "/api/v1/completion/",
        {
            "messages": [{"role": "user", "content": "Hi"}],
            "task_type": "SCHEDULED_AUTOMATION",
        },
        format="json",
    )
    assert response.status_code == 404


def execute_job_task_delay(job):
    from apps.jobs.tasks import execute_job_task

    return execute_job_task.delay(str(job.id)).get()
