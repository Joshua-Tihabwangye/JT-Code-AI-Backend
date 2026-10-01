"""Graph registry and LangGraph builders.

Each registered graph is a bounded LangGraph ``StateGraph`` whose nodes only
act through the :class:`~apps.agents.context.ExecutionContext`:

* every model call passes the budget gate, goes through the AI gateway and is traced;
* every tool call passes the budget gate and the tool-selection policy, its
  output is wrapped and scanned as untrusted data, and it is traced.

Graphs: ``direct_answer`` (one model turn, no tools) and ``research``
(ReAct loop over the run's permitted read-only tools).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, START, MessagesState, StateGraph

from apps.agents import safety
from apps.agents.context import ExecutionContext
from apps.ai_gateway.adapters import ChatMessage, ToolCall

AgentGraph = StateGraph[MessagesState, None, MessagesState, MessagesState]


class Node(Protocol):
    """A LangGraph node over the shared message state."""

    def __call__(self, state: MessagesState) -> dict[str, Any]: ...


DEFAULT_SYSTEM_PROMPT = (
    "You are JT-Code's assistant. Answer accurately and concisely. When you use the knowledge "
    "base or other tools, cite the source titles you relied on."
)


# --------------------------------------------------------------------------- tool policy


ALL_TOOLS = "*"


def tool_catalog(organization_id: Any = None) -> dict[str, Any]:
    """Every tool the registry can resolve (static tools plus the tenant's MCP tools)."""
    from apps.tools.registry import specs_for_organization, static_specs

    return specs_for_organization(organization_id) if organization_id is not None else static_specs()


def select_tools(
    *,
    graph: str,
    agent_tools: list[str] | tuple[str, ...] | None = None,
    requested: list[str] | tuple[str, ...] | None = None,
    organization_id: Any = None,
    user: Any = None,
) -> tuple[str, ...]:
    """Tools a run may use: registered ∩ graph ∩ tenant-enabled ∩ role ∩ agent ∩ requested.

    Unknown names are dropped, so a client or model can never widen the set. The
    tool gateway re-checks every condition at call time.
    """
    from apps.tools.gateway import _role_rank, is_enabled, tenant_policy
    from apps.tools.registry import ROLE_RANK

    spec = GRAPHS[graph]
    catalog = tool_catalog(organization_id)
    names = sorted(catalog) if spec.default_tools == (ALL_TOOLS,) else list(spec.default_tools)
    allowed = [name for name in names if name in catalog]
    if organization_id is not None:
        allowed = [
            name
            for name in allowed
            if is_enabled(catalog[name], organization_id, tenant_policy(organization_id, name))
        ]
        if user is not None:
            rank = _role_rank(user, organization_id)
            allowed = [name for name in allowed if rank >= ROLE_RANK[catalog[name].min_role]]
    if agent_tools is not None:
        allowed = [name for name in allowed if name in set(agent_tools)]
    if requested is not None:
        allowed = [name for name in allowed if name in set(requested)]
    return tuple(allowed)


def tool_definitions(ctx: ExecutionContext) -> list[dict[str, Any]]:
    catalog = tool_catalog(ctx.organization_id)
    return [catalog[name].tool_def() for name in ctx.tools if name in catalog]


# --------------------------------------------------------------------------- nodes


def _gateway_messages(ctx: ExecutionContext, messages: Sequence[BaseMessage]) -> list[ChatMessage]:
    system = ctx.system_prompt or DEFAULT_SYSTEM_PROMPT
    if ctx.tools:
        system = f"{system}\n\n{safety.UNTRUSTED_DATA_POLICY}"
    converted = [ChatMessage("system", system)]
    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, AIMessage):
            calls = tuple(
                ToolCall(id=str(call.get("id") or ""), name=call["name"], arguments=call.get("args") or {})
                for call in message.tool_calls or []
            )
            converted.append(ChatMessage("assistant", str(message.content or ""), tool_calls=calls))
        elif isinstance(message, ToolMessage):
            converted.append(
                ChatMessage(
                    "tool",
                    str(message.content or ""),
                    tool_call_id=str(message.tool_call_id),
                    name=str(message.name or ""),
                )
            )
        else:
            converted.append(ChatMessage("user", str(message.content or "")))
    return converted


def call_model_node(ctx: ExecutionContext) -> Node:
    def call_model(state: MessagesState) -> dict[str, Any]:
        ctx.before_model_call()
        from apps.ai_gateway.service import generate_completion

        generate = ctx.generate or generate_completion
        started = time.monotonic()
        outcome = generate(
            messages=_gateway_messages(ctx, state["messages"]),
            task_type=ctx.task_type,
            model_alias=ctx.model_alias,
            temperature=ctx.temperature,
            max_tokens=ctx.budget.max_output_tokens,
            tools=tool_definitions(ctx) if ctx.tools else None,
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            job_id=ctx.job_id,
            organization_id=ctx.organization_id,
        )
        ctx.usage.model_calls += 1
        ctx.usage.input_tokens += outcome.usage.input_tokens
        ctx.usage.output_tokens += outcome.usage.output_tokens
        run_cost = getattr(outcome.run, "estimated_cost_usd", None)
        if run_cost is None:
            run_cost = getattr(outcome.run, "provider_cost_usd", 0)
        ctx.usage.cost_usd += run_cost or 0
        ctx.model_runs.append(str(outcome.run.id))
        # Only tools permitted for this run may be requested back into the graph.
        calls = [
            {"name": call.name, "args": call.arguments, "id": call.id or f"call-{index}", "type": "tool_call"}
            for index, call in enumerate(outcome.tool_calls)
        ]
        ctx.step(
            "call_model",
            "model",
            summary=f"{len(outcome.content)} chars, {len(calls)} tool call(s)",
            detail={
                "modelAlias": getattr(outcome, "model_alias", "") or ctx.model_alias or "",
                "toolCalls": [call["name"] for call in calls],
                "inputTokens": outcome.usage.input_tokens,
                "outputTokens": outcome.usage.output_tokens,
            },
            model_run_id=str(outcome.run.id),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return {"messages": [AIMessage(content=outcome.content, tool_calls=calls)]}

    return call_model


REPLAYED = "replayed"


def default_tool_executor(
    ctx: ExecutionContext, name: str, arguments: dict[str, Any], call_id: str
) -> tuple[str, str]:
    """Ephemeral (in-process) runs: built-in read-only tools only, no persistence."""
    from apps.agents.tools import invoke_tool

    return "ok", invoke_tool(name, user=ctx.user, organization_id=ctx.organization_id, arguments=arguments)


def governed_tool_executor(
    ctx: ExecutionContext, name: str, arguments: dict[str, Any], call_id: str
) -> tuple[str, str]:
    """Durable runs: every call goes through the tool gateway (policy, approval, audit)."""
    from apps.agents.context import AgentAwaitingApproval
    from apps.tools.gateway import execute_tool
    from apps.tools.models import ToolInvocation

    result = execute_tool(
        name,
        arguments,
        user=ctx.user,
        organization_id=ctx.organization_id,
        source=ToolInvocation.Source.AGENT,
        agent_run=ctx.run,
        tool_call_id=call_id,
        tainted=ctx.tainted,
        trace_id=ctx.trace_id,
    )
    if result.status == ToolInvocation.Status.PENDING_APPROVAL and result.approval is not None:
        ctx.step(
            "execute_tools",
            "tool",
            outcome="blocked",
            summary=f"{name}: awaiting approval",
            detail={"tool": name, "approvalId": str(result.approval.id), "awaitingApproval": True},
        )
        raise AgentAwaitingApproval(str(result.approval.id))
    replayed = ctx.is_replay(call_id)
    outcomes: dict[str, str] = {
        ToolInvocation.Status.SUCCEEDED: "ok",
        ToolInvocation.Status.DENIED: "blocked",
        ToolInvocation.Status.REJECTED: "blocked",
        ToolInvocation.Status.FAILED: "failed",
    }
    outcome = outcomes.get(result.status, "failed")
    return (REPLAYED if replayed else outcome), result.content


ToolExecutor = Callable[[ExecutionContext, str, dict[str, Any], str], tuple[str, str]]


def execute_tools_node(ctx: ExecutionContext, executor: ToolExecutor | None = None) -> Node:
    run_tool = executor or default_tool_executor

    def execute_tools(state: MessagesState) -> dict[str, Any]:
        last = state["messages"][-1]
        outputs: list[ToolMessage] = []
        pending = last.tool_calls if isinstance(last, AIMessage) else []
        for call in pending:
            name = str(call.get("name", ""))
            call_id = str(call.get("id") or "call-0")
            arguments = call.get("args") or {}
            started = time.monotonic()
            if name not in ctx.tools:
                ctx.before_tool_call()
                outcome, content = "blocked", f"Tool {name!r} is not permitted for this run."
            else:
                if not ctx.is_replay(call_id):
                    ctx.before_tool_call()
                outcome, content = run_tool(ctx, name, arguments, call_id)
            findings = safety.scan(content)
            if outcome == REPLAYED:
                # Already executed, counted and traced before the run paused.
                if findings:
                    ctx.tainted = True
                outputs.append(
                    ToolMessage(
                        content=safety.wrap_untrusted(f"tool:{name}", content),
                        tool_call_id=call_id,
                        name=name,
                    )
                )
                continue
            ctx.usage.tool_calls += 1
            if outcome == "ok":
                ctx.invoked_tools.append(name)
            if findings:
                ctx.taint(f"tool:{name}", [f.rule for f in findings])
                safety.record_safety_event(
                    organization_id=ctx.organization_id,
                    user=ctx.user,
                    findings=findings,
                    blocked=False,
                    text=content,
                    source=f"tool output ({name})",
                    trace_id=ctx.trace_id,
                    request_id=ctx.request_id,
                )
            ctx.step(
                "execute_tools",
                "tool",
                outcome="ok" if outcome == "ok" else ("failed" if outcome == "failed" else "blocked"),
                summary=f"{name}: {outcome}",
                detail={
                    "tool": name,
                    "callId": call_id,
                    "argsDigest": safety.digest(repr(sorted(arguments.items()))),
                    "outputChars": len(content),
                    "injectionRules": [f.rule for f in findings],
                },
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            outputs.append(
                ToolMessage(
                    content=safety.wrap_untrusted(f"tool:{name}", content), tool_call_id=call_id, name=name
                )
            )
        return {"messages": outputs}

    return execute_tools


def _route_after_model(state: MessagesState) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "execute_tools"
    return END


# --------------------------------------------------------------------------- builders


def build_direct_answer(ctx: ExecutionContext, executor: ToolExecutor | None = None) -> AgentGraph:
    builder: AgentGraph = StateGraph(MessagesState)
    builder.add_node("call_model", call_model_node(ctx))
    builder.add_edge(START, "call_model")
    builder.add_edge("call_model", END)
    return builder


def build_research(ctx: ExecutionContext, executor: ToolExecutor | None = None) -> AgentGraph:
    builder: AgentGraph = StateGraph(MessagesState)
    builder.add_node("call_model", call_model_node(ctx))
    builder.add_node("execute_tools", execute_tools_node(ctx, executor))
    builder.add_edge(START, "call_model")
    builder.add_conditional_edges(
        "call_model", _route_after_model, {"execute_tools": "execute_tools", END: END}
    )
    builder.add_edge("execute_tools", "call_model")
    return builder


@dataclass(frozen=True)
class GraphSpec:
    name: str
    description: str
    builder: Callable[..., AgentGraph]
    default_tools: tuple[str, ...]
    task_type: str
    model_alias: str


GRAPHS: dict[str, GraphSpec] = {
    "direct_answer": GraphSpec(
        name="direct_answer",
        description="Single model turn without tools.",
        builder=build_direct_answer,
        default_tools=(),
        task_type="GENERAL_QUESTION",
        model_alias="default-chat",
    ),
    "tool_agent": GraphSpec(
        name="tool_agent",
        description=(
            "Tool-using agent over every tool the tenant enabled (integrations and MCP); "
            "side-effecting calls pause for human approval."
        ),
        builder=lambda ctx, executor=None: build_research(ctx, executor),
        default_tools=(ALL_TOOLS,),
        task_type="SEARCH_RESEARCH",
        model_alias="tool-calling",
    ),
    "research": GraphSpec(
        name="research",
        description="Tool-using research loop over the organization's read-only tools.",
        builder=build_research,
        default_tools=("knowledge.search", "system.now", "identity.whoami"),
        task_type="SEARCH_RESEARCH",
        model_alias="tool-calling",
    ),
}


def initial_messages(text: str) -> list[AnyMessage]:
    return [HumanMessage(content=text)]
