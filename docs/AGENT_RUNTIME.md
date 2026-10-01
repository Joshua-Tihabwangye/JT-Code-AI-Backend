# LangGraph agent runtime (Phase 8)

Agents run as bounded LangGraph graphs executed by Celery workers. Every model call goes
through the AI gateway (Phase 7); every run is persisted, traced, evaluated and resumable.

## Flow

```
POST /agents/{id}/runs  or  POST /agent-runs     (Idempotency-Key required)
  → authorize (editor/admin of the tenant) → rate limit (THROTTLE_AGENT_RUNS)
  → tenant concurrency cap (MAX_CONCURRENT_AGENT_RUNS_PER_TENANT, 429)
  → intent router → graph + tool policy + budget snapshot → AgentRun(queued)
  → worker: input safety gate → graph (checkpointed) → finalize + evaluate + events
```

## Components

| Module | Responsibility |
| --- | --- |
| `graphs.py` | Graph registry (`direct_answer`, `research`), node implementations, tool-selection policy |
| `router.py` | Intent router: deterministic rules (default) or `AGENT_ROUTER_MODE=model` via the `classification` alias with rules fallback; a pinned agent graph always wins |
| `context.py` | `ExecutionContext`, `Budget`, cancellation/deadline checks, step tracing |
| `safety.py` | Input gate (jailbreak/injection detection), untrusted-content wrapping, `SafetyEvent` recording |
| `checkpoint.py` | `DjangoCheckpointSaver` — LangGraph checkpoints in PostgreSQL keyed by run id |
| `engine.py` | Create, claim, execute/resume, finalize, evaluate, cancel |
| `tasks.py` | `execute_agent_run` (queue `jobs.analysis`), `recover_stalled_agent_runs` (Beat, 60s) |

## Bounded execution

A run's budget is snapshotted at creation from the agent definition, **clamped** to the
platform ceilings (`LANGGRAPH_MAX_STEPS`, `AGENT_MAX_ITERATIONS`, `AGENT_MAX_TOOL_CALLS`,
`AGENT_MAX_COST_USD`, `AGENT_MAX_DURATION_SECONDS`). Gates run **before** every model call
(iteration, cost), **before** every tool call (tool-call count) and at every node
(cancellation, wall-clock deadline); every traced step counts toward the step limit, and
LangGraph's recursion limit is a second backstop. Exceeding any limit ends the run as
`failed` with a specific code (`AGENT_MAX_ITERATIONS_EXCEEDED`, `AGENT_BUDGET_EXCEEDED`,
`AGENT_STEP_LIMIT_EXCEEDED`, `AGENT_DEADLINE_EXCEEDED`).

## Tool-selection policy

Permitted tools = registered tools ∩ the graph's tools ∩ the agent's `allowedTools` ∩ the
request's `tools`. Clients and agents can only narrow the set. A model request for any
other tool is answered with "not permitted", traced as `blocked`, and never executed.

## Safety

- **Input gate**: unambiguous jailbreaks (revealing/overriding the system prompt, "developer
  mode", fake system turns, credential exfiltration) block the run before any model call
  (`AGENT_SAFETY_BLOCKED`); weaker signals are recorded as flags.
- **Untrusted content**: tool output is wrapped in `<untrusted_data>` delimiters (nested
  delimiters are neutralized) and the system prompt instructs the model to treat it as
  data. Injection indicators in tool output **taint** the run and create a `SafetyEvent`
  for review; Phase 9 blocks side-effecting tools on tainted runs.
- Safety events store a SHA-256 and a 200-character excerpt, never the full text.

## Durability and cancellation

- The graph is compiled with `DjangoCheckpointSaver` (`thread_id` = run id). A worker crash
  leaves the run `running`; `recover_stalled_agent_runs` re-queues it once its
  `heartbeat_at` is older than `AGENT_RUN_STALLED_TIMEOUT_SECONDS`, and the new worker
  **resumes from the last checkpoint** (completed tool steps are not repeated). After
  `AGENT_RUN_MAX_ATTEMPTS` the run fails instead of looping.
- Checkpoints are deleted when a run is terminal, so conversation state is not retained
  beyond the run.
- `POST /agent-runs/{id}/cancel/` ends queued/paused runs immediately; a running run stops
  at its next node boundary.

## Traces, evaluation and events

- `AgentStep`: one row per node (`input_gate`, `call_model`, `execute_tools`) with outcome,
  summary, safe detail (tool name, argument digest, token counts), linked `ModelRun` and
  latency. `GET /agent-runs/{id}/steps/` returns it; `GET /agent-runs/{id}/events/` streams
  it as SSE (`step` events with `id: <sequence>`, resumable via `Last-Event-ID`, then a
  terminal `completed`/`failed`/`cancelled` event).
- `AgentEvaluation` (`heuristic-v1`): terminated normally, answered, within budget, grounded
  when retrieving, safety flags. Failing evaluations are the review queue.
- Outbox events: `agents.run.started`, `agents.run.completed`, `agents.run.failed`,
  `agents.run.cancelled`.

## Visibility

Runs are visible to the user who started them and to administrators of the organization.
Starting and cancelling runs requires editor or admin access to the tenant.
