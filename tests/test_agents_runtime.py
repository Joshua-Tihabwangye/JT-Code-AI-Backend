"""Tests for the LangGraph agent runtime, tool registry and SEARCH_RESEARCH executor path."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.test import override_settings
from langchain_core.messages import HumanMessage

from apps.agents.runtime import (
    AgentMaxIterationsError,
    run_agent,
)
from apps.agents.tools import (
    Tool,
    default_agent_tools,
    get_tool,
    invoke_tool,
    register_tool,
    tool_defs,
)
from apps.ai_gateway.adapters import ToolCall, Usage
from apps.ai_gateway.service import GenerationOutcome
from apps.events.models import OutboxEvent
from apps.identity.models import Organization
from apps.jobs.executor import execute_job
from apps.jobs.models import Job, JobStep


@pytest.fixture
def org(user):
    org = Organization.objects.create(name="Agent Org", owner=user)
    user.organizations.add(org)
    return org


@pytest.fixture
def credit_balance(user, org):
    from apps.billing.services import CreditService

    wallet = CreditService.get_or_create_wallet(org)
    CreditService.add_credits(wallet, 10000, reason="Test credits")
    return wallet


def _make_job(user, org, task_type, payload, **kwargs):
    return Job.objects.create(
        owner=user,
        organization=org,
        task_type=task_type,
        trace_id="t-agent-1",
        input_payload=payload,
        **kwargs,
    )


def _fake_model(name="echo-chat"):
    return SimpleNamespace(name=name, id="00000000-0000-0000-0000-000000000001")


def _fake_run(id: str = "run-1") -> SimpleNamespace:
    return SimpleNamespace(id=id, provider_cost_usd=Decimal("0"))


def _make_outcome(*, content="", tool_calls=(), run_id="run-1"):
    return GenerationOutcome(
        content=content,
        model=_fake_model(),
        provider=SimpleNamespace(type="echo"),
        policy=None,
        run=_fake_run(id=run_id),
        messages=[],
        usage=Usage(input_tokens=10, output_tokens=8),
        tool_calls=tuple(tool_calls),
    )


# --- Tool registry ---


def test_default_agent_tools_registered():
    tools = default_agent_tools()
    assert "knowledge.search" in tools
    assert "system.now" in tools
    assert "identity.whoami" in tools


def test_tool_defs_returns_descriptions():
    defs = tool_defs(["system.now"])
    assert len(defs) == 1
    assert defs[0]["name"] == "system.now"


def test_invoke_unknown_tool_returns_error_message():
    result = invoke_tool("bogus", user=None, organization_id=None, arguments={})
    assert "not registered" in result


def test_system_now_returns_iso_timestamp():
    tool = get_tool("system.now")
    result = tool.handler(user=None, organization_id=None)
    assert "T" in result


def test_whoami_returns_user_info():
    user = SimpleNamespace(display_name="Alice", email="alice@example.com")
    result = invoke_tool("identity.whoami", user=user, organization_id=None, arguments={})
    assert "Alice" in result
    assert "alice@example.com" in result


def test_register_custom_tool(monkeypatch):
    sentinel = {}

    def _sentinel_handler(user, organization_id, **kwargs):  # noqa: ARG001
        sentinel["called"] = True
        return "ok"

    tool = Tool(
        name="test.custom",
        description="Custom test tool",
        parameters={"type": "object", "properties": {}},
        handler=_sentinel_handler,
    )
    register_tool(tool)
    assert get_tool("test.custom") is tool
    result = invoke_tool("test.custom", user=None, organization_id=None, arguments={})
    assert result == "ok"
    assert sentinel["called"]


def test_invoke_tool_catches_exception():
    def _raising(user, organization_id, **kwargs):  # noqa: ARG001
        raise RuntimeError("boom")

    register_tool(Tool(name="test.raise", description="", parameters={}, handler=_raising))
    result = invoke_tool("test.raise", user=None, organization_id=None, arguments={})
    assert "boom" in result


# --- Runtime (scripted gateway) ---


@pytest.mark.django_db
@override_settings(AI_PROVIDER="echo")
def test_run_agent_tool_call_then_answer():
    calls = {"n": 0}

    def scripted(**kwargs):
        n = calls["n"]
        calls["n"] += 1
        if n == 0:
            return _make_outcome(
                tool_calls=(ToolCall(id="c1", name="system.now", arguments={}),),
            )
        return _make_outcome(content="The time is now.")

    import apps.agents.runtime as runtime

    original = runtime.generate_completion
    runtime.generate_completion = scripted
    try:
        run = run_agent(
            user=None,
            organization_id=None,
            initial_messages=[HumanMessage(content="what time is it?")],
            tools=("system.now",),
            max_model_calls=5,
        )
    finally:
        runtime.generate_completion = original

    assert run.final_answer == "The time is now."
    assert run.model_runs == ["run-1", "run-1"]
    assert run.invoked_tools == ["system.now"]
    assert run.input_tokens == 20
    assert run.output_tokens == 16


@pytest.mark.django_db
@override_settings(AI_PROVIDER="echo")
def test_run_agent_direct_answer_no_tools():
    def scripted(**kwargs):
        return _make_outcome(content="No tools needed.")

    import apps.agents.runtime as runtime

    original = runtime.generate_completion
    runtime.generate_completion = scripted
    try:
        run = run_agent(
            user=None,
            organization_id=None,
            initial_messages=[HumanMessage(content="hello")],
            tools=(),
            max_model_calls=3,
        )
    finally:
        runtime.generate_completion = original

    assert run.final_answer == "No tools needed."
    assert run.invoked_tools == []
    assert run.model_runs == ["run-1"]


@pytest.mark.django_db
@override_settings(AI_PROVIDER="echo")
def test_run_agent_raises_on_max_iterations():
    def always_tool(**kwargs):
        return _make_outcome(tool_calls=(ToolCall(id="c1", name="system.now", arguments={}),))

    import apps.agents.runtime as runtime

    original = runtime.generate_completion
    runtime.generate_completion = always_tool
    try:
        with pytest.raises(AgentMaxIterationsError) as exc_info:
            run_agent(
                user=None,
                organization_id=None,
                initial_messages=[HumanMessage(content="loop")],
                tools=("system.now",),
                max_model_calls=2,
            )
        assert "AGENT_MAX_ITERATIONS_EXCEEDED" in exc_info.value.code
    finally:
        runtime.generate_completion = original


@pytest.mark.django_db
def test_run_agent_events_aggreated():
    calls = {"n": 0}

    def scripted(**kwargs):
        n = calls["n"]
        calls["n"] += 1
        if n == 0:
            return _make_outcome(
                run_id="r1",
                tool_calls=(ToolCall(id="c1", name="system.now", arguments={}),),
            )
        return _make_outcome(run_id="r2", content="done")

    import apps.agents.runtime as runtime

    original = runtime.generate_completion
    runtime.generate_completion = scripted
    try:
        run = run_agent(
            user=None,
            organization_id=None,
            initial_messages=[HumanMessage(content="hi")],
            tools=("system.now",),
        )
        assert len(run.events) >= 1
    finally:
        runtime.generate_completion = original


# --- SEARCH_RESEARCH executor ---


@pytest.mark.django_db
@override_settings(AI_PROVIDER="echo")
def test_execute_search_research_completes(user, org, credit_balance, monkeypatch):
    from apps.agents import runtime as agent_runtime

    job = _make_job(user, org, Job.TaskType.SEARCH_RESEARCH, {"query": "test query"})
    call_args = {}

    def fake_run_agent(**kwargs):
        call_args.update(kwargs)

        class FakeRun:
            final_answer = "research result"
            invoked_tools = ["knowledge.search"]
            model_runs = ["mr-1"]
            input_tokens = 15
            output_tokens = 20
            messages = []

        return FakeRun()

    monkeypatch.setattr(agent_runtime, "run_agent", fake_run_agent)
    result = execute_job(job)
    assert result["status"] == "completed"
    assert result["task_type"] == Job.TaskType.SEARCH_RESEARCH
    job.refresh_from_db()
    assert job.status == Job.Status.COMPLETED
    assert job.result["answer"] == "research result"
    assert job.result["grounded"] is True
    assert job.result["tools"] == ["knowledge.search"]
    step = JobStep.objects.get(job=job)
    assert step.status == JobStep.Status.COMPLETED
    assert OutboxEvent.objects.filter(topic__endswith="jobs.job.completed").exists()


@pytest.mark.django_db
def test_execute_search_research_failure_records_outbox(user, org, credit_balance, monkeypatch):
    from apps.agents import runtime as agent_runtime
    from apps.ai_gateway.adapters import AIGatewayError

    job = _make_job(user, org, Job.TaskType.SEARCH_RESEARCH, {"query": "test query"})

    def raising(**kwargs):
        raise AIGatewayError("fail", code="GATEWAY_FAIL")

    monkeypatch.setattr(agent_runtime, "run_agent", raising)
    result = execute_job(job)
    assert result["status"] == "failed"
    assert result["error_code"] == "GATEWAY_FAIL"
    job.refresh_from_db()
    assert job.status == Job.Status.FAILED
    assert OutboxEvent.objects.filter(topic__endswith="jobs.job.failed").exists()


@pytest.mark.django_db
@override_settings(AI_PROVIDER="echo")
def test_search_research_policy_seed_allows_api(authenticated_client, user, org, credit_balance):
    response = authenticated_client.post(
        "/api/v1/completion/",
        {
            "messages": [{"role": "user", "content": "find info about cats"}],
            "task_type": "SEARCH_RESEARCH",
        },
        format="json",
    )
    assert response.status_code == 202
    job = Job.objects.get(id=response.data["job_id"])
    assert job.status == Job.Status.COMPLETED
    assert job.result["answer"]
