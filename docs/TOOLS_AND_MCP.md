# Tools, MCP and integrations (Phase 9)

Every tool call — from an agent run or `POST /api/v1/tools/{name}/execute/` — passes through
one enforcement point, `apps.tools.gateway.execute_tool`, and is recorded as a
`ToolInvocation`. Nothing outside the registry can execute.

## Gateway checks (in order)

| # | Check | Deny code |
| --- | --- | --- |
| 1 | Tool exists (static registry or an allowlisted MCP tool of the tenant) | `UNKNOWN_TOOL` |
| 2 | Enabled for the tenant (`TenantToolPolicy`; integrations are off by default) | `TOOL_DISABLED` |
| 3 | Caller is a member with the required role; side effects always need editor+ | `NOT_A_MEMBER`, `FORBIDDEN_ROLE` |
| 4 | Arguments match the JSON schema (unknown keys rejected, size-capped) | `INVALID_ARGUMENTS` |
| 5 | Side effects refused on runs tainted by prompt injection | `TAINTED_CONTEXT` |
| 6 | Side effects need a human approval bound to the exact arguments | pending / `APPROVAL_MISMATCH` / `APPROVAL_ALREADY_USED` |
| 7 | Adapter rules (allowlists, branch policy, egress) | e.g. `REPOSITORY_NOT_ALLOWED`, `EGRESS_DENIED` |

Denials and executed side effects also create a governance `AuditEvent`. Arguments are
stored redacted (secret-looking keys, long strings) with a SHA-256 digest; agent-run outputs
are stored (capped) only so a resumed run can replay them instead of re-executing.

## Human approval

A side-effecting call creates a `ToolApproval` (tool, exact arguments, digest, expiry
`TOOL_APPROVAL_TTL_SECONDS`). Nothing runs until an editor or admin decides:

- `POST /api/v1/tool-approvals/{id}/approve/` or `/reject/` (`{"note": "..."}`).
- **Agent runs** pause as `waiting_approval` (graph state is checkpointed). A decision resumes
  the run from its checkpoint: earlier tool results are replayed, the approved call executes
  exactly once, and a rejection is reported to the model as "not performed".
- **Direct API calls** return `202 {approvalId}`; approving executes the stored call once.
- An approval is single-use, tenant-bound and argument-bound; `expire_tool_approvals`
  (Beat, 5 min) expires stale ones and resumes their runs. Cancelling a paused run expires
  its approvals.

## Prompt-injection defences

1. Tool output is wrapped in `<untrusted_data>` and the system prompt forbids following
   instructions inside it (Phase 8).
2. Output is scanned; injection indicators **taint** the run (`AgentRun.tainted`, persisted
   across pauses) and create a `SafetyEvent` for review.
3. On a tainted run every side-effecting call is **denied** (`TAINTED_CONTEXT`) — not even an
   approval is offered.
4. Side effects never run without a human decision, and egress/allowlists bound what any
   call can reach.

## Adapters

| Tools | Credential (`ToolCredential`, encrypted) | Least-privilege rules |
| --- | --- | --- |
| `github.list_repositories`, `github.read_file`, `github.list_tree` (read); `github.create_branch`, `github.commit_file`, `github.create_pull_request` (approval) | `github`: `metadata.installation_id`, `metadata.repositories` (allowlist); App key in `GITHUB_APP_ID`/`GITHUB_APP_PRIVATE_KEY` | Installation tokens are minted per call for **one repository** with **minimum permissions** (`contents: read` for reads). Writes only on new branches under `TOOL_GITHUB_BRANCH_PREFIX`; never `main`/`master`/default. |
| `slack.list_channels` (read); `slack.post_message` (approval) | `slack`: bot token (`xoxb-`) as secret, `metadata.allowed_channels` | Channel allowlist; `@channel/@here/@everyone` neutralized. |
| `web.fetch`, `web.search` | none (`SEARCH_API_KEY` for search) | Disabled unless `BROWSER_TOOL_ENABLED` or a tenant policy enables them; optional `config.allowed_domains`; text content types only. |
| `http.request` (GET/HEAD read; other methods need approval) | `http`: `metadata.base_url` (HTTPS), `allowed_methods`, `allowed_path_prefixes`, `auth_header`; secret = header value | Host pinned to the connection; path traversal and absolute URLs rejected; method allowlist. |
| `mcp.<server>.<tool>` | optional `mcp` credential (`metadata.auth_header`) | See MCP below. |

Credentials are encrypted with Fernet (`TOOL_CREDENTIALS_ENCRYPTION_KEYS`, comma-separated;
the first key encrypts, all decrypt, so keys rotate without downtime). Secrets are
write-only through `/api/v1/tool-credentials/` and hidden in Django admin. Credential
lookups are always filtered by the calling tenant.

## Egress policy (`apps.tools.egress`)

HTTPS on the default port only; no credentials in URLs; no IP literals; the host is
resolved and **every** address must be public (private, loopback, link-local/metadata
`169.254.169.254`, reserved and multicast are refused, including IPv4-mapped IPv6); the
connection is pinned to the validated IP with TLS verified against the hostname (defeats
DNS rebinding); redirects are re-validated hop by hop; `TOOL_EGRESS_DENYLIST` applies;
responses are capped at `TOOL_MAX_RESPONSE_BYTES`; requests time out after
`EXTERNAL_API_TIMEOUT_SECONDS`.

## MCP

1. An admin registers a server: `POST /api/v1/mcp/servers/` (`slug`, `name`, public HTTPS
   `url`, optional `credentialId`).
2. `POST /api/v1/mcp/servers/{id}/discover/` runs `initialize` + `tools/list` (Streamable HTTP,
   JSON or SSE responses, session id honoured) and stores `discoveredTools`.
3. Nothing is callable until the admin sets `allowedTools`. Allowlisted tools appear as
   `mcp.<server>.<tool>` and are **side-effecting (approval required)** unless also listed in
   `readOnlyTools` — server-provided hints are not trusted.
4. Each call re-checks that the server is active and the tool still allowlisted.
   `ENABLE_MCP=false` disables all MCP tools.

## Agent integration

- Graph `tool_agent` (router intent for GitHub/Slack/API actions) may use every tool the
  tenant enabled and the caller's role allows; `research` stays read-only.
- Run tools = registered ∩ graph ∩ tenant-enabled ∩ role ∩ agent ∩ request; a model request
  for anything else is traced as `blocked` and never reaches the gateway.

## Administration endpoints (admin role)

`/api/v1/tool-policies/`, `/api/v1/tool-credentials/`, `/api/v1/mcp/servers/`,
`/api/v1/tool-invocations/` (audit). `GET /api/v1/tools/` lists what the caller may use.
