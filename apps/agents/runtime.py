"""LangGraph ReAct agent runtime driven by the AI gateway.

The runtime is a classic tool-calling loop: the model is invoked through the
AI gateway (so policies, fallback and ``ModelRun`` usage tracking apply on
every step), any requested tool call is executed against the server-side tool
registry, and the result is fed back until the model stops requesting tools.

Streaming is supported via ``iter_agent`` (LangGraph ``stream_mode='updates'``);
``run_agent`` drains it into a final :class:`AgentRun`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, START, MessagesState, StateGraph

from apps.agents.tools import invoke_tool, tool_defs
from apps.ai_gateway.adapters import AIGatewayError
from apps.ai_gateway.adapters import ChatMessage as GatewayMessage
from apps.ai_gateway.service import generate_completion

AGENT_DEFAULT_SYSTEM_PROMPT = (
    "You are JT-Code's research agent. Use the available tools to gather "
    "information before answering. Answer concisely and cite the source "
    "document titles when you use the knowledge base."
)


class AgentRuntimeError(AIGatewayError):
    code = "AGENT_RUNTIME_ERROR"


class AgentMaxIterationsError(AgentRuntimeError):
    code = "AGENT_MAX_ITERATIONS_EXCEEDED"


@dataclass
class AgentRun:
    """Outcome of a completed agent run."""

    messages: list[BaseMessage] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    invoked_tools: list[str] = field(default_factory=list)
    model_runs: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def final_answer(self) -> str:
        for message in reversed(self.messages):
            if isinstance(message, AIMessage) and message.content:
                return str(message.content) if not message.tool_calls else ""
        return ""


class _AgentContext:
    def __init__(
        self,
        *,
        user,
        organization_id,
        task_type: str,
        model_id: str | None,
        policy_slug: str | None,
        temperature: float,
        max_tokens: int | None,
        tools: tuple[str, ...],
        system: str | None,
        max_model_calls: int,
        request_id: str | None,
        trace_id: str,
        job_id: str | None,
    ):
        self.user = user
        self.organization_id = organization_id
        self.task_type = task_type
        self.model_id = model_id
        self.policy_slug = policy_slug
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.tools = tools
        self.system = system
        self.max_model_calls = max_model_calls
        self.request_id = request_id
        self.trace_id = trace_id
        self.job_id = job_id
        self.model_calls = 0
        self.model_runs: list[str] = []
        self.invoked_tools: list[str] = []
        self.input_tokens = 0
        self.output_tokens = 0


def _to_gateway_message(message: BaseMessage) -> GatewayMessage:
    if isinstance(message, SystemMessage):
        role = "system"
    elif isinstance(message, HumanMessage):
        role = "user"
    elif isinstance(message, AIMessage):
        role = "assistant"
    elif isinstance(message, ToolMessage):
        role = "tool"
    else:
        role = "user"
    return GatewayMessage(role=role, content=str(message.content or ""))


def _tool_call_stanza(call: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": call.get("name", ""),
        "args": call.get("args") or {},
        "id": call.get("id", ""),
        "type": "tool_call",
    }


def _build_graph(ctx: _AgentContext):
    def call_model(state: dict[str, Any]) -> dict[str, Any]:
        if ctx.model_calls >= ctx.max_model_calls:
            raise AgentMaxIterationsError(f"Agent exceeded the {ctx.max_model_calls}-call iteration limit.")
        gateway_messages = [_to_gateway_message(m) for m in state["messages"]]
        if ctx.system:
            gateway_messages.insert(0, GatewayMessage("system", ctx.system))

        outcome = generate_completion(
            messages=gateway_messages,
            task_type=ctx.task_type,
            model_id=ctx.model_id,
            policy_slug=ctx.policy_slug,
            temperature=ctx.temperature,
            max_tokens=ctx.max_tokens,
            tools=tool_defs(ctx.tools),
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            job_id=ctx.job_id,
            organization_id=ctx.organization_id,
        )
        ctx.model_calls += 1
        ctx.model_runs.append(str(outcome.run.id))
        ctx.input_tokens += outcome.usage.input_tokens
        ctx.output_tokens += outcome.usage.output_tokens

        if outcome.tool_calls:
            ai = AIMessage(
                content=outcome.content,
                tool_calls=[
                    _tool_call_stanza({"name": tc.name, "args": tc.arguments, "id": tc.id})
                    for tc in outcome.tool_calls
                ],
            )
        else:
            ai = AIMessage(content=outcome.content)
        return {"messages": [ai]}

    def execute_tools(state: dict[str, Any]) -> dict[str, Any]:
        last = state["messages"][-1]
        outputs = []
        for call in last.tool_calls or []:
            name = call.get("name", "")
            args = call.get("args") or {}
            ctx.invoked_tools.append(name)
            try:
                content = invoke_tool(
                    name,
                    user=ctx.user,
                    organization_id=ctx.organization_id,
                    arguments=args,
                )
            except Exception as exc:  # noqa: BLE001 - tool failures feed back to the model
                content = f"Tool {name!r} failed: {exc}"
            outputs.append(ToolMessage(content=content, tool_call_id=str(call.get("id") or "call-0")))
        return {"messages": outputs}

    def route(state: dict[str, Any]) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            return "execute_tools"
        return END

    builder = StateGraph(MessagesState)
    builder.add_node("call_model", call_model)
    builder.add_node("execute_tools", execute_tools)
    builder.add_edge(START, "call_model")
    builder.add_conditional_edges("call_model", route, {"execute_tools": "execute_tools", END: END})
    builder.add_edge("execute_tools", "call_model")
    return builder.compile()


def iter_agent(
    *,
    user,
    organization_id,
    initial_messages: list[BaseMessage],
    task_type: str = "GENERAL_QUESTION",
    model_id: str | None = None,
    policy_slug: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    tools: tuple[str, ...] = (),
    system: str | None = AGENT_DEFAULT_SYSTEM_PROMPT,
    max_model_calls: int = 6,
    request_id: str | None = None,
    trace_id: str = "",
    job_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream agent updates; each event is ``{node_name: {channel: value}}``."""
    ctx = _AgentContext(
        user=user,
        organization_id=organization_id,
        task_type=task_type,
        model_id=model_id,
        policy_slug=policy_slug,
        temperature=temperature,
        max_tokens=max_tokens,
        tools=tools,
        system=system,
        max_model_calls=max_model_calls,
        request_id=request_id,
        trace_id=trace_id,
        job_id=job_id,
    )
    graph = _build_graph(ctx)
    state: dict[str, Any] = {"messages": list(initial_messages)}
    yield from graph.stream(state, config={"recursion_limit": max_model_calls * 4})

    yield {
        "summary": {
            "model_calls": ctx.model_calls,
            "model_runs": ctx.model_runs,
            "invoked_tools": ctx.invoked_tools,
            "input_tokens": ctx.input_tokens,
            "output_tokens": ctx.output_tokens,
        }
    }


def run_agent(
    *,
    user,
    organization_id,
    initial_messages: list[BaseMessage],
    task_type: str = "GENERAL_QUESTION",
    model_id: str | None = None,
    policy_slug: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    tools: tuple[str, ...] = (),
    system: str | None = AGENT_DEFAULT_SYSTEM_PROMPT,
    max_model_calls: int = 6,
    request_id: str | None = None,
    trace_id: str = "",
    job_id: str | None = None,
) -> AgentRun:
    """Run the agent to completion and return the final transcript."""
    run = AgentRun(events=[])
    for event in iter_agent(
        user=user,
        organization_id=organization_id,
        initial_messages=initial_messages,
        task_type=task_type,
        model_id=model_id,
        policy_slug=policy_slug,
        temperature=temperature,
        max_tokens=max_tokens,
        tools=tools,
        system=system,
        max_model_calls=max_model_calls,
        request_id=request_id,
        trace_id=trace_id,
        job_id=job_id,
    ):
        if "summary" in event:
            summary = event["summary"]
            run.model_runs = summary["model_runs"]
            run.invoked_tools = summary["invoked_tools"]
            run.input_tokens = summary["input_tokens"]
            run.output_tokens = summary["output_tokens"]
            continue
        run.events.append(event)
        for payload in event.values():
            if not isinstance(payload, dict):
                continue
            for message in payload.get("messages", []):
                if message not in run.messages:
                    run.messages.append(message)
    if not run.messages:
        raise AgentRuntimeError("Agent produced no messages.", code="AGENT_EMPTY_RUN")
    if not isinstance(run.messages[-1], AIMessage):
        raise AgentRuntimeError("Agent run did not terminate with the model.", code="AGENT_BAD_TERMINATION")
    return run
