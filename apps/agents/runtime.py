"""In-process agent execution on the registered graphs (no persistence).

Durable, traced, cancellable runs go through :mod:`apps.agents.engine`. This
module keeps the original ``run_agent``/``iter_agent`` API for callers that
need a synchronous, ephemeral run; it uses the same graphs, gates and budgets.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, cast

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import MessagesState

from apps.agents.context import (
    AgentMaxIterationsError,
    AgentRuntimeError,
    Budget,
    ExecutionContext,
    MemoryRecorder,
)
from apps.agents.graphs import GRAPHS, tool_catalog
from apps.ai_gateway.service import generate_completion  # noqa: F401 - resolved via globals() at call time

__all__ = [
    "AGENT_DEFAULT_SYSTEM_PROMPT",
    "AgentMaxIterationsError",
    "AgentResult",
    "AgentRun",
    "AgentRuntimeError",
    "iter_agent",
    "run_agent",
]

AGENT_DEFAULT_SYSTEM_PROMPT = (
    "You are JT-Code's research agent. Use the available tools to gather "
    "information before answering. Answer concisely and cite the source "
    "document titles when you use the knowledge base."
)


@dataclass
class AgentResult:
    """Outcome of a completed in-process agent run."""

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


# Backward-compatible name for the in-process result type.
AgentRun = AgentResult


def _generate(**kwargs: Any) -> Any:
    # Resolved at call time so tests can substitute ``runtime.generate_completion``.
    return globals()["generate_completion"](**kwargs)


def iter_agent(
    *,
    user: Any,
    organization_id: Any,
    initial_messages: list[BaseMessage],
    task_type: str = "GENERAL_QUESTION",
    model_id: str | None = None,
    policy_slug: str | None = None,
    model_alias: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    tools: tuple[str, ...] = (),
    system: str | None = AGENT_DEFAULT_SYSTEM_PROMPT,
    max_model_calls: int = 6,
    request_id: str | None = None,
    trace_id: str = "",
    job_id: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream graph updates; the final event is ``{"summary": {...}}``."""
    registered = tool_catalog()
    ctx = ExecutionContext(
        user=user,
        organization_id=organization_id,
        graph="research",
        budget=Budget(
            max_steps=max_model_calls * 4 + 4,
            max_model_calls=max_model_calls,
            max_tool_calls=max_model_calls * 4,
            max_output_tokens=max_tokens,
        ),
        recorder=MemoryRecorder(),
        tools=tuple(name for name in tools if name in registered),
        task_type=task_type,
        model_alias=model_alias,
        system_prompt=system,
        temperature=temperature,
        request_id=request_id,
        trace_id=trace_id,
        job_id=job_id,
        generate=_generate,
    )
    graph = GRAPHS["research"].builder(ctx).compile()
    config: RunnableConfig = {"recursion_limit": ctx.budget.max_steps * 2 + 4}
    state: MessagesState = {"messages": cast(list[AnyMessage], list(initial_messages))}
    yield from graph.stream(state, config=config)
    yield {
        "summary": {
            "model_calls": ctx.usage.model_calls,
            "model_runs": ctx.model_runs,
            "invoked_tools": ctx.invoked_tools,
            "input_tokens": ctx.usage.input_tokens,
            "output_tokens": ctx.usage.output_tokens,
        }
    }


def run_agent(**kwargs: Any) -> AgentResult:
    """Run the research graph in-process to completion and return the transcript."""
    result = AgentResult()
    for event in iter_agent(**kwargs):
        if "summary" in event:
            summary = event["summary"]
            result.model_runs = summary["model_runs"]
            result.invoked_tools = summary["invoked_tools"]
            result.input_tokens = summary["input_tokens"]
            result.output_tokens = summary["output_tokens"]
            continue
        result.events.append(event)
        for payload in event.values():
            if isinstance(payload, dict):
                for message in payload.get("messages", []):
                    if message not in result.messages:
                        result.messages.append(message)
    if not result.messages:
        raise AgentRuntimeError("Agent produced no messages.", code="AGENT_EMPTY_RUN")
    if not isinstance(result.messages[-1], AIMessage):
        raise AgentRuntimeError("Agent run did not terminate with the model.", code="AGENT_BAD_TERMINATION")
    return result
