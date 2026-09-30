"""Phase 7: AI gateway and model registry.

Provider adapters are exercised against mock HTTP transports so every fault
path (timeouts, 429/Retry-After, 5xx, auth, safety blocks, malformed streams)
is deterministic. Exit criterion: a provider swap requires no client API change.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from django.core.cache import cache

from apps.ai_gateway import resilience
from apps.ai_gateway.adapters import AIProviderNotConfigured, ChatMessage, ToolCall
from apps.ai_gateway.models import Model, ModelAlias, ModelAliasTarget, ModelRun, Provider
from apps.ai_gateway.providers import base as provider_base
from apps.ai_gateway.providers.base import (
    ContentBlocked,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    error_for_response,
    resolve_api_key,
)
from apps.ai_gateway.providers.gemini import GeminiChatAdapter
from apps.ai_gateway.providers.gemini import build_request as gemini_request
from apps.ai_gateway.providers.llama import LlamaChatAdapter
from apps.ai_gateway.registry import ModelSelectionError, resolve_candidates
from apps.ai_gateway.service import BudgetExceeded, generate_completion, stream_completion
from apps.identity.models import Organization
from apps.jobs.models import Job

GEMINI_HOST = "gemini.test"
LLAMA_HOST = "llama.test"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def gateway_settings(settings, monkeypatch):
    settings.GEMINI_API_KEY = "gemini-test-key"
    settings.LLAMA_API_KEY = "llama-test-key"
    settings.LLAMA_API_BASE = f"https://{LLAMA_HOST}/v1"
    settings.AI_GATEWAY_MAX_COST_USD = 10.0
    settings.AI_GATEWAY_MAX_LATENCY_MS = 30000
    settings.AI_GATEWAY_FALLBACK_ENABLED = True
    settings.AI_MAX_RETRIES = 2
    settings.AI_RETRY_BASE_SECONDS = 0.01
    settings.AI_RETRY_MAX_BACKOFF_SECONDS = 2
    settings.AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 30
    cache.clear()
    sleeps: list[float] = []
    monkeypatch.setattr(resilience, "sleep", sleeps.append)
    return sleeps


class Router:
    """Mock transport routing by host; each handler returns an ``httpx.Response``."""

    def __init__(self):
        self.handlers = {}
        self.calls: list[httpx.Request] = []

    def on(self, host, handler):
        self.handlers[host] = handler
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        handler = self.handlers.get(request.url.host)
        if handler is None:
            return httpx.Response(599, json={"error": "unrouted"})
        return handler(request)

    def calls_to(self, host):
        return [call for call in self.calls if call.url.host == host]


@pytest.fixture
def router(monkeypatch):
    routes = Router()
    monkeypatch.setattr(
        provider_base,
        "http_client",
        lambda timeout: httpx.Client(transport=httpx.MockTransport(routes), timeout=timeout),
    )
    return routes


def gemini_ok(text="Hello from Gemini", calls=(), usage=(12, 5)):
    parts = [{"text": text}] if text else []
    parts += [{"functionCall": {"name": name, "args": args}} for name, args in calls]
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": usage[0], "candidatesTokenCount": usage[1]},
        },
    )


def llama_ok(text="Hello from Llama", tool_calls=None, usage=(20, 7)):
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(
        200,
        json={
            "choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]},
        },
    )


@pytest.fixture
def providers(db):
    gemini = Provider.objects.create(
        name="Gemini (test)",
        slug="test-gemini",
        type=Provider.Type.GOOGLE,
        base_url=f"https://{GEMINI_HOST}/v1beta",
        circuit_breaker_threshold=2,
        max_retries=2,
    )
    llama = Provider.objects.create(
        name="Llama (test)",
        slug="test-llama",
        type=Provider.Type.LLAMA,
        base_url=f"https://{LLAMA_HOST}/v1",
        max_retries=2,
    )
    common = {
        "modality": Model.Modality.TEXT,
        "supports_tools": True,
        "supports_streaming": True,
        "max_output_tokens": 1000,
    }
    gemini_model = Model.objects.create(
        provider=gemini,
        name="gemini-test",
        display_name="Gemini test",
        input_price_per_token=Decimal("0.000001"),
        output_price_per_token=Decimal("0.000002"),
        **common,
    )
    llama_model = Model.objects.create(
        provider=llama,
        name="llama-test",
        display_name="Llama test",
        input_price_per_token=Decimal("0.0000001"),
        output_price_per_token=Decimal("0.0000001"),
        **common,
    )
    alias = ModelAlias.objects.create(slug="test-chat", description="Test chat alias")
    ModelAliasTarget.objects.create(alias=alias, model=gemini_model, priority=0)
    ModelAliasTarget.objects.create(alias=alias, model=llama_model, priority=1)
    return {"gemini": gemini_model, "llama": llama_model, "alias": alias}


@pytest.fixture
def org(user):
    organization = Organization.objects.create(name="Gateway Org", owner=user)
    user.organizations.add(organization)
    return organization


def ask(**kwargs):
    kwargs.setdefault("model_alias", "test-chat")
    kwargs.setdefault("messages", [ChatMessage("user", "Hi")])
    return generate_completion(**kwargs)


# --------------------------------------------------------------------------- adapters


def test_gemini_request_maps_roles_system_tools_and_safety():
    body = gemini_request(
        [
            ChatMessage("system", "Be brief."),
            ChatMessage("user", "Find docs"),
            ChatMessage(
                "assistant",
                "",
                tool_calls=(ToolCall(id="c1", name="knowledge.search", arguments={"query": "x"}),),
            ),
            ChatMessage("tool", "result text", tool_call_id="c1", name="knowledge.search"),
        ],
        temperature=0.2,
        max_tokens=64,
        tools=[{"name": "knowledge.search", "description": "Search", "parameters": {"type": "object"}}],
    )

    assert body["systemInstruction"] == {"parts": [{"text": "Be brief."}]}
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert body["contents"][1]["parts"][0]["functionCall"]["name"] == "knowledge__search"
    assert body["contents"][2]["parts"][0]["functionResponse"]["name"] == "knowledge__search"
    assert body["tools"][0]["functionDeclarations"][0]["name"] == "knowledge__search"
    assert body["generationConfig"] == {"temperature": 0.2, "maxOutputTokens": 64}
    assert len(body["safetySettings"]) == 4


@pytest.mark.django_db
def test_gemini_generate_normalizes_text_usage_and_tool_calls(router, providers):
    router.on(
        GEMINI_HOST, lambda request: gemini_ok("Checking", calls=[("knowledge__search", {"query": "q"})])
    )

    result = GeminiChatAdapter().generate(messages=[ChatMessage("user", "q")], model=providers["gemini"])

    request = router.calls[0]
    assert request.url.path.endswith("/models/gemini-test:generateContent")
    assert request.headers["x-goog-api-key"] == "gemini-test-key"
    assert result.content == "Checking"
    assert result.tool_calls[0].name == "knowledge.search"
    assert result.tool_calls[0].arguments == {"query": "q"}
    assert result.finish_reason == "tool_calls"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 5)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body",
    [
        {"promptFeedback": {"blockReason": "SAFETY"}},
        {"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]},
    ],
)
def test_gemini_safety_block_is_normalized(router, providers, body):
    router.on(GEMINI_HOST, lambda request: httpx.Response(200, json=body))

    with pytest.raises(ContentBlocked):
        GeminiChatAdapter().generate(messages=[ChatMessage("user", "q")], model=providers["gemini"])


@pytest.mark.django_db
def test_llama_generate_uses_openai_schema_with_encoded_tools(router, providers):
    router.on(
        LLAMA_HOST,
        lambda request: llama_ok(
            "",
            tool_calls=[
                {
                    "id": "call-9",
                    "type": "function",
                    "function": {"name": "system__now", "arguments": "{}"},
                }
            ],
        ),
    )

    result = LlamaChatAdapter().generate(
        messages=[ChatMessage("user", "time?"), ChatMessage("tool", "12:00", tool_call_id="call-1")],
        model=providers["llama"],
        tools=[{"name": "system.now", "description": "Now", "parameters": {"type": "object"}}],
    )

    sent = json.loads(router.calls[0].content)
    assert router.calls[0].headers["Authorization"] == "Bearer llama-test-key"
    assert sent["model"] == "llama-test"
    assert sent["tools"][0]["function"]["name"] == "system__now"
    assert sent["messages"][1] == {"role": "tool", "tool_call_id": "call-1", "content": "12:00"}
    assert result.tool_calls == (ToolCall(id="call-9", name="system.now", arguments={}),)
    assert result.finish_reason == "tool_calls"


@pytest.mark.django_db
def test_llama_stream_reassembles_tool_call_fragments(router, providers):
    events = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c", "function": {"name": "system__"}}]}}]},
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"name": "now", "arguments": "{}"}}]}}
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 3}},
    ]
    sse = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    router.on(LLAMA_HOST, lambda request: httpx.Response(200, text=sse))

    chunks = list(LlamaChatAdapter().stream(messages=[ChatMessage("user", "hi")], model=providers["llama"]))

    assert "".join(chunk.delta for chunk in chunks) == "Hello"
    final = chunks[-1]
    assert final.tool_calls[0].name == "system.now"
    assert final.finish_reason == "tool_calls"
    assert final.usage.input_tokens == 3


@pytest.mark.django_db
def test_gemini_stream_yields_deltas_and_usage(router, providers):
    events = [
        {"candidates": [{"content": {"parts": [{"text": "Hi "}]}}]},
        {
            "candidates": [{"content": {"parts": [{"text": "there"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2},
        },
    ]
    sse = "".join(f"data: {json.dumps(event)}\r\n\r\n" for event in events)
    router.on(GEMINI_HOST, lambda request: httpx.Response(200, text=sse))

    chunks = list(GeminiChatAdapter().stream(messages=[ChatMessage("user", "hi")], model=providers["gemini"]))

    assert "".join(chunk.delta for chunk in chunks) == "Hi there"
    assert chunks[-1].usage.output_tokens == 2
    assert router.calls[0].url.params["alt"] == "sse"


@pytest.mark.django_db
def test_llama_refuses_plaintext_remote_endpoints(providers, settings):
    settings.DEBUG = False
    providers["llama"].provider.base_url = "http://llama.internal/v1"

    with pytest.raises(AIProviderNotConfigured):
        LlamaChatAdapter().generate(messages=[ChatMessage("user", "hi")], model=providers["llama"])


@pytest.mark.parametrize(
    ("status", "headers", "error"),
    [
        (429, {"Retry-After": "3"}, ProviderRateLimited),
        (503, {}, ProviderUnavailable),
        (504, {}, ProviderTimeout),
        (401, {}, ProviderAuthError),
        (400, {}, ProviderBadRequest),
    ],
)
def test_http_errors_map_to_the_gateway_taxonomy(status, headers, error):
    response = httpx.Response(status, headers=headers, json={"error": {"message": "nope"}})

    mapped = error_for_response(response, "gemini")

    assert isinstance(mapped, error)
    if status == 429:
        assert mapped.retry_after == 3.0


def test_credentials_ref_cannot_read_arbitrary_secrets(settings):
    settings.DJANGO_SECRET_KEY = "super-secret"
    provider = Provider(slug="evil", type=Provider.Type.GOOGLE, credentials_ref="DJANGO_SECRET_KEY")

    with pytest.raises(AIProviderNotConfigured):
        resolve_api_key(provider, "GEMINI_API_KEY")


# --------------------------------------------------------------------------- resilience & fallback


@pytest.mark.django_db
def test_transient_failure_is_retried_on_the_same_model(router, providers, gateway_settings):
    responses = iter([httpx.Response(503, json={"error": {"message": "busy"}}), gemini_ok()])
    router.on(GEMINI_HOST, lambda request: next(responses))

    outcome = ask()

    assert outcome.model == providers["gemini"]
    assert outcome.fallback_used is False
    assert outcome.run.retry_count == 1
    assert len(gateway_settings) == 1
    assert router.calls_to(LLAMA_HOST) == []


@pytest.mark.django_db
def test_retry_after_is_honoured_or_triggers_fallback(router, providers, gateway_settings):
    responses = iter([httpx.Response(429, headers={"Retry-After": "1"}), gemini_ok()])
    router.on(GEMINI_HOST, lambda request: next(responses))
    ask()
    assert gateway_settings == [1.0]

    cache.clear()
    gateway_settings.clear()
    router.on(GEMINI_HOST, lambda request: httpx.Response(429, headers={"Retry-After": "120"}))
    router.on(LLAMA_HOST, lambda request: llama_ok())

    outcome = ask()

    assert outcome.model == providers["llama"]
    assert gateway_settings == []


@pytest.mark.django_db
def test_timeout_falls_back_to_the_next_compatible_model(router, providers, org):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    router.on(GEMINI_HOST, timeout)
    router.on(LLAMA_HOST, lambda request: llama_ok("From Llama"))

    outcome = ask(organization_id=org.id, trace_id="trace-7", request_id=None)

    run = ModelRun.objects.get(id=outcome.run.id)
    assert outcome.content == "From Llama"
    assert run.fallback_used is True
    assert run.model == providers["llama"]
    assert run.model_alias == "test-chat"
    assert run.organization_id == org.id
    assert [attempt["provider"] for attempt in run.metadata["attempts"]] == ["test-gemini", "test-llama"]
    assert run.metadata["attempts"][0]["error_code"] == ProviderTimeout.code


@pytest.mark.django_db
def test_content_block_is_final_and_never_shops_for_another_model(router, providers):
    router.on(
        GEMINI_HOST, lambda request: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    )
    router.on(LLAMA_HOST, lambda request: llama_ok())

    with pytest.raises(ContentBlocked):
        ask()

    assert router.calls_to(LLAMA_HOST) == []
    assert ModelRun.objects.get().status == ModelRun.Status.FAILED


@pytest.mark.django_db
def test_circuit_breaker_opens_skips_and_recovers(router, providers, settings):
    settings.AI_MAX_RETRIES = 0
    router.on(GEMINI_HOST, lambda request: httpx.Response(503, json={"error": {"message": "down"}}))
    router.on(LLAMA_HOST, lambda request: llama_ok())

    ask()
    ask()  # second consecutive failure trips the threshold of 2
    gemini_calls = len(router.calls_to(GEMINI_HOST))
    outcome = ask()

    assert len(router.calls_to(GEMINI_HOST)) == gemini_calls  # skipped while open
    assert outcome.run.metadata["attempts"][0]["error_code"] == "PROVIDER_CIRCUIT_OPEN"

    # After the cooldown the breaker is half-open: one trial call, success closes it.
    breaker = resilience.CircuitBreaker.for_provider(providers["gemini"].provider)
    cache.set(breaker._open_key, 0, timeout=60)
    router.on(GEMINI_HOST, lambda request: gemini_ok())
    outcome = ask()
    assert outcome.model == providers["gemini"]
    assert breaker.state() == "closed"


@pytest.mark.django_db
def test_circuit_breaker_fails_open_when_the_cache_is_down(router, providers, monkeypatch):
    def broken(*_args, **_kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(resilience.cache, "get", broken)
    router.on(GEMINI_HOST, lambda request: gemini_ok())

    assert ask().model == providers["gemini"]


@pytest.mark.django_db
def test_capability_filter_skips_models_without_tool_support(router, providers):
    Model.objects.filter(id=providers["gemini"].id).update(supports_tools=False)
    router.on(LLAMA_HOST, lambda request: llama_ok())

    outcome = ask(tools=[{"name": "system.now", "description": "", "parameters": {"type": "object"}}])

    assert outcome.model == providers["llama"]
    assert router.calls_to(GEMINI_HOST) == []

    Model.objects.filter(id=providers["llama"].id).update(supports_tools=False)
    with pytest.raises(ModelSelectionError) as excinfo:
        resolve_candidates(model_alias="test-chat", required_capabilities={"tools"})
    assert excinfo.value.code == "NO_CAPABLE_MODEL"


@pytest.mark.django_db
def test_cost_ceiling_skips_expensive_models_and_blocks_when_none_fit(router, providers, settings):
    router.on(LLAMA_HOST, lambda request: llama_ok())
    settings.AI_GATEWAY_MAX_COST_USD = 0.0005  # gemini max ≈ $0.002, llama ≈ $0.0001

    assert ask().model == providers["llama"]
    assert router.calls_to(GEMINI_HOST) == []

    settings.AI_GATEWAY_MAX_COST_USD = 0.00000001
    with pytest.raises(BudgetExceeded):
        ask()


def test_retry_gives_up_when_backoff_would_exceed_the_deadline(monkeypatch, gateway_settings):
    attempts = []

    def flaky():
        attempts.append(1)
        raise ProviderUnavailable("down")

    monkeypatch.setattr(resilience, "backoff_seconds", lambda attempt: 5.0)

    with pytest.raises(ProviderUnavailable):
        resilience.call_with_retry(flaky, max_retries=5, deadline=resilience.Deadline(1000))
    assert attempts == [1]  # a 5s backoff cannot fit in a 1s deadline, so no retry
    assert gateway_settings == []


@pytest.mark.django_db
def test_completed_run_records_usage_cost_latency_and_alias(router, providers, org):
    router.on(GEMINI_HOST, lambda request: gemini_ok(usage=(1000, 500)))

    outcome = ask(organization_id=org.id, trace_id="trace-cost")

    run = ModelRun.objects.get(id=outcome.run.id)
    assert (run.input_tokens, run.output_tokens) == (1000, 500)
    assert run.estimated_cost_usd == Decimal("0.00200000")  # 1000*1e-6 + 500*2e-6
    assert run.latency_ms is not None
    assert run.trace_id == "trace-cost"
    assert run.status == ModelRun.Status.COMPLETED


@pytest.mark.django_db
def test_stream_falls_back_before_the_first_token_only(router, providers):
    router.on(GEMINI_HOST, lambda request: httpx.Response(503, json={"error": {"message": "down"}}))
    sse = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
    router.on(LLAMA_HOST, lambda request: httpx.Response(200, text=sse))

    events = list(stream_completion(messages=[ChatMessage("user", "hi")], model_alias="test-chat"))

    assert [event["type"] for event in events] == ["delta", "done"]
    assert events[0]["text"] == "ok"
    assert events[-1]["modelAlias"] == "test-chat"
    assert ModelRun.objects.get().fallback_used is True


@pytest.mark.django_db
def test_mid_stream_failure_is_final(router, providers):
    good = 'data: {"candidates":[{"content":{"parts":[{"text":"partial"}]}}]}\n\ndata: {not json}\n\n'
    router.on(GEMINI_HOST, lambda request: httpx.Response(200, text=good))
    router.on(LLAMA_HOST, lambda request: llama_ok())

    stream = stream_completion(messages=[ChatMessage("user", "hi")], model_alias="test-chat")
    assert next(stream)["text"] == "partial"
    with pytest.raises(Exception):  # noqa: B017 - any normalized provider error ends the stream
        list(stream)

    assert router.calls_to(LLAMA_HOST) == []
    assert ModelRun.objects.get().status == ModelRun.Status.FAILED


# --------------------------------------------------------------------------- API & exit criterion


@pytest.fixture
def funded(org):
    from apps.billing.services import CreditService

    wallet = CreditService.get_or_create_wallet(org)
    CreditService.add_credits(wallet, 10000, reason="Phase 7 tests")
    return wallet


def complete(client, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        return client.post(
            "/api/v1/completion/",
            {"messages": [{"role": "user", "content": "Hello"}], "model_alias": "test-chat"},
            format="json",
        )


@pytest.mark.django_db
def test_provider_swap_requires_no_client_api_change(
    authenticated_client, router, providers, funded, django_capture_on_commit_callbacks
):
    """Exit criterion: re-pointing an alias changes the provider, not the client contract."""
    router.on(GEMINI_HOST, lambda request: gemini_ok("gemini answer"))
    router.on(LLAMA_HOST, lambda request: llama_ok("llama answer"))

    before = complete(authenticated_client, django_capture_on_commit_callbacks)
    ModelAliasTarget.objects.filter(alias=providers["alias"], model=providers["gemini"]).delete()
    after = complete(authenticated_client, django_capture_on_commit_callbacks)

    assert before.status_code == after.status_code == 202
    assert set(before.json()) == set(after.json())
    assert before.json()["modelAlias"] == after.json()["modelAlias"] == "test-chat"
    first = Job.objects.get(id=before.json()["job_id"])
    second = Job.objects.get(id=after.json()["job_id"])
    assert (first.result["answer"], second.result["answer"]) == ("gemini answer", "llama answer")
    assert set(first.result) == set(second.result)
    assert first.result["usage"]["model_alias"] == second.result["usage"]["model_alias"] == "test-chat"


@pytest.mark.django_db
def test_unknown_alias_is_rejected_before_any_work(authenticated_client, providers, funded):
    response = authenticated_client.post(
        "/api/v1/completion/",
        {"messages": [{"role": "user", "content": "Hi"}], "model_alias": "no-such-alias"},
        format="json",
    )

    assert response.status_code == 404
    assert not Job.objects.exists()


@pytest.mark.django_db
def test_capability_registry_is_public_and_secret_free(authenticated_client, providers):
    response = authenticated_client.get("/api/v1/system/capabilities/")

    body = response.json()
    assert response.status_code == 200
    alias = next(item for item in body["aliases"] if item["alias"] == "test-chat")
    assert alias["available"] is True
    assert {"chat", "tools", "streaming"} <= set(alias["capabilities"])
    rendered = json.dumps(body)
    assert "gemini-test" not in rendered and "llama-test" not in rendered
    assert "gemini-test-key" not in rendered


@pytest.mark.django_db
def test_model_alias_listing_hides_provider_models(authenticated_client, providers):
    response = authenticated_client.get("/api/v1/model-aliases/")

    assert response.status_code == 200
    slugs = [item["slug"] for item in response.json()["results"]]
    assert "test-chat" in slugs
    assert "gemini-test" not in json.dumps(response.json())


def test_production_requires_a_real_model_provider(monkeypatch):
    from config.settings.validation import validate_environment
    from tests.test_phase1_foundations import _strict_env

    for key, value in _strict_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AI_PROVIDER", "echo")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("LLAMA_API_KEY", "")
    monkeypatch.setenv("LLAMA_API_BASE", "http://llama.example.com/v1")

    problems = validate_environment("production")

    assert any("AI_PROVIDER=echo" in problem for problem in problems)
    assert any("GEMINI_API_KEY or LLAMA_API_KEY" in problem for problem in problems)
    assert any("LLAMA_API_BASE must use HTTPS" in problem for problem in problems)
