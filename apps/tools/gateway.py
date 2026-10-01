"""The tool gateway: the single enforcement and audit point for every tool call.

Checks, in order (any failure is a *denied*, audited invocation):

1. the tool exists (static registry or the tenant's allowlisted MCP tool);
2. it is enabled for the tenant;
3. the caller belongs to the tenant with the tool's required role
   (side-effecting calls always need editor or admin);
4. arguments satisfy the tool's JSON schema (unknown keys rejected);
5. side-effecting calls are refused on runs tainted by prompt injection;
6. side-effecting calls need a human approval bound to the exact arguments;
7. execution with a timeout; output is capped and recorded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import jsonschema
import sentry_sdk
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.tools.models import TenantToolPolicy, ToolApproval, ToolInvocation
from apps.tools.registry import ROLE_RANK, ToolContext, ToolSpec, get_spec

logger = logging.getLogger(__name__)

SECRET_KEY_HINTS = (
    "token",
    "secret",
    "password",
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "cookie",
)


class ToolDenied(PermissionError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ToolExecutionError(RuntimeError):
    """A tool ran and failed (transport error, upstream rejection, bad response)."""


@dataclass
class ToolResult:
    status: str  # ToolInvocation.Status value
    content: str
    invocation: ToolInvocation
    approval: ToolApproval | None = None

    @property
    def ok(self) -> bool:
        return self.status == ToolInvocation.Status.SUCCEEDED


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def redact(value: Any, *, key: str = "") -> Any:
    """Redact secret-looking keys and bound long strings for the audit log."""
    if any(hint in key.lower() for hint in SECRET_KEY_HINTS):
        return "[redacted]"
    if isinstance(value, dict):
        return {k: redact(v, key=str(k)) for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        return [redact(v) for v in value[:50]]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + f"…[{len(value) - 500} more chars]"
    return value


def _role_rank(user: Any, organization_id: Any) -> int:
    from apps.identity.authorization import user_has_role
    from apps.identity.models import Role

    if not user.organizations.filter(id=organization_id).exists():
        return -1
    for role, rank in ((Role.RoleType.ADMIN, 2), (Role.RoleType.EDITOR, 1)):
        if user_has_role(user, role, organization_id):
            return rank
    return 0


def tenant_policy(organization_id: Any, name: str) -> TenantToolPolicy | None:
    return TenantToolPolicy.objects.filter(organization_id=organization_id, tool_name=name).first()


def is_enabled(spec: ToolSpec, organization_id: Any, policy: TenantToolPolicy | None) -> bool:
    if spec.name.startswith("mcp."):
        return True  # MCP tools exist only when allowlisted on an active server
    return policy.enabled if policy is not None else spec.default_enabled


def validate_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> None:
    if not isinstance(arguments, dict):
        raise ToolDenied("INVALID_ARGUMENTS", "Tool arguments must be an object.")
    if len(json.dumps(arguments, default=str)) > settings.TOOL_MAX_ARGUMENT_BYTES:
        raise ToolDenied("INVALID_ARGUMENTS", "Tool arguments are too large.")
    schema = dict(spec.parameters or {"type": "object"})
    if spec.strict_schema and schema.get("type") == "object":
        schema.setdefault("additionalProperties", False)
    try:
        jsonschema.validate(arguments, schema, cls=jsonschema.Draft202012Validator)
    except jsonschema.ValidationError as exc:
        raise ToolDenied("INVALID_ARGUMENTS", f"Invalid arguments: {exc.message}") from None


def _record(
    *,
    spec_name: str,
    user: Any,
    organization_id: Any,
    source: str,
    arguments: dict[str, Any],
    status: str,
    side_effect: bool,
    agent_run: Any = None,
    tool_call_id: str = "",
    deny_code: str = "",
    output: str = "",
    error: str = "",
    approval: ToolApproval | None = None,
    latency_ms: int | None = None,
    trace_id: str = "",
) -> ToolInvocation:
    invocation = ToolInvocation.objects.create(
        organization_id=organization_id,
        user=user if getattr(user, "pk", None) else None,
        agent_run=agent_run,
        tool_call_id=tool_call_id,
        source=source,
        tool_name=spec_name[:150],
        side_effect=side_effect,
        status=status,
        deny_code=deny_code,
        arguments_redacted=redact(arguments),
        arguments_digest=digest(arguments),
        output=output[: settings.TOOL_MAX_OUTPUT_CHARS] if agent_run is not None else "",
        output_digest=digest(output) if output else "",
        output_chars=len(output),
        error=error[:2000],
        approval=approval,
        latency_ms=latency_ms,
        trace_id=trace_id,
    )
    if status in {ToolInvocation.Status.DENIED, ToolInvocation.Status.REJECTED} or (
        side_effect and status == ToolInvocation.Status.SUCCEEDED
    ):
        _audit(invocation)
    return invocation


def _audit(invocation: ToolInvocation) -> None:
    from apps.governance.models import AuditEvent

    denied = invocation.status == ToolInvocation.Status.DENIED
    AuditEvent.objects.create(
        organization_id=invocation.organization_id,
        actor=invocation.user,
        category=AuditEvent.Category.SECURITY if denied else AuditEvent.Category.DATA_MODIFICATION,
        action=f"tool.{invocation.status}",
        resource_type="tool",
        resource_id=invocation.tool_name,
        severity=AuditEvent.Severity.MEDIUM if denied else AuditEvent.Severity.LOW,
        description=f"Tool {invocation.tool_name} {invocation.status} {invocation.deny_code}".strip(),
        metadata={"invocationId": str(invocation.id), "source": invocation.source},
        trace_id=invocation.trace_id,
    )


def _run(spec: ToolSpec, ctx: ToolContext, arguments: dict[str, Any]) -> str:
    """Run the handler on the calling thread.

    Every outbound request is bounded by the egress layer's connect/read
    timeouts and response-size cap, so no extra thread (and no extra
    per-thread database connection) is needed to bound a call.
    """
    return str(spec.handler(ctx, arguments))


def _prior_success(agent_run: Any, tool_call_id: str) -> ToolInvocation | None:
    if agent_run is None or not tool_call_id:
        return None
    return (
        ToolInvocation.objects.filter(
            agent_run=agent_run, tool_call_id=tool_call_id, status=ToolInvocation.Status.SUCCEEDED
        )
        .order_by("-created_at")
        .first()
    )


def _approval_for(
    *,
    spec: ToolSpec,
    arguments: dict[str, Any],
    organization_id: Any,
    user: Any,
    agent_run: Any,
    tool_call_id: str,
    approval_id: Any,
) -> tuple[ToolApproval, bool]:
    """Return ``(approval, created)``: the bound approval for this call, creating a pending one."""
    args_digest = digest(arguments)
    if approval_id is not None:
        approval = ToolApproval.objects.filter(id=approval_id, organization_id=organization_id).first()
        if approval is None or approval.tool_name != spec.name or approval.arguments_digest != args_digest:
            raise ToolDenied("APPROVAL_MISMATCH", "The approval does not match this tool call.")
        return approval, False
    if agent_run is not None and tool_call_id:
        existing = ToolApproval.objects.filter(agent_run=agent_run, tool_call_id=tool_call_id).first()
        if existing is not None:
            if existing.arguments_digest != args_digest or existing.tool_name != spec.name:
                raise ToolDenied("APPROVAL_MISMATCH", "The approval does not match this tool call.")
            return existing, False
    approval = ToolApproval.objects.create(
        organization_id=organization_id,
        requested_by=user,
        agent_run=agent_run,
        tool_call_id=tool_call_id,
        tool_name=spec.name,
        arguments=arguments,
        arguments_digest=args_digest,
        summary=f"{spec.name}({', '.join(sorted(arguments))})"[:500],
        expires_at=timezone.now() + timedelta(seconds=settings.TOOL_APPROVAL_TTL_SECONDS),
    )
    return approval, True


def execute_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    user: Any,
    organization_id: Any,
    source: str,
    agent_run: Any = None,
    tool_call_id: str = "",
    tainted: bool = False,
    approval_id: Any = None,
    trace_id: str = "",
) -> ToolResult:
    """Authorize, approve-gate, execute and audit one tool call."""
    arguments = arguments or {}
    if (prior := _prior_success(agent_run, tool_call_id)) is not None:
        return ToolResult(ToolInvocation.Status.SUCCEEDED, prior.output, prior)
    spec = get_spec(name, organization_id)
    side_effect = spec.is_side_effect(arguments) if spec else False
    record = {
        "spec_name": name,
        "user": user,
        "organization_id": organization_id,
        "source": source,
        "arguments": arguments,
        "side_effect": side_effect,
        "agent_run": agent_run,
        "tool_call_id": tool_call_id,
        "trace_id": trace_id,
    }
    policy = None
    try:
        if spec is None:
            raise ToolDenied("UNKNOWN_TOOL", f"Tool {name!r} does not exist.")
        policy = tenant_policy(organization_id, name)
        if not is_enabled(spec, organization_id, policy):
            raise ToolDenied("TOOL_DISABLED", f"Tool {name!r} is not enabled for this organization.")
        rank = _role_rank(user, organization_id)
        if rank < 0:
            raise ToolDenied("NOT_A_MEMBER", "The caller is not a member of this organization.")
        if rank < ROLE_RANK[spec.required_role(arguments)]:
            raise ToolDenied(
                "FORBIDDEN_ROLE", f"Tool {name!r} requires the {spec.required_role(arguments)} role."
            )
        validate_arguments(spec, arguments)
        if side_effect and tainted:
            raise ToolDenied(
                "TAINTED_CONTEXT",
                "Side-effecting tools are blocked: this run processed content with injection indicators.",
            )
    except ToolDenied as denial:
        invocation = _record(
            **record, status=ToolInvocation.Status.DENIED, deny_code=denial.code, error=str(denial)
        )
        return ToolResult(ToolInvocation.Status.DENIED, f"Denied ({denial.code}): {denial}", invocation)

    approval = None
    if side_effect:
        with transaction.atomic():
            try:
                approval, created = _approval_for(
                    spec=spec,
                    arguments=arguments,
                    organization_id=organization_id,
                    user=user,
                    agent_run=agent_run,
                    tool_call_id=tool_call_id,
                    approval_id=approval_id,
                )
            except ToolDenied as denial:
                invocation = _record(
                    **record, status=ToolInvocation.Status.DENIED, deny_code=denial.code, error=str(denial)
                )
                return ToolResult(
                    ToolInvocation.Status.DENIED, f"Denied ({denial.code}): {denial}", invocation
                )
            approval = ToolApproval.objects.select_for_update().get(id=approval.id)
            if approval.status == ToolApproval.Status.PENDING and approval.expires_at <= timezone.now():
                approval.status = ToolApproval.Status.EXPIRED
                approval.save(update_fields=["status"])
            if approval.status in {ToolApproval.Status.PENDING}:
                invocation = _record(
                    **record, status=ToolInvocation.Status.PENDING_APPROVAL, approval=approval
                )
                return ToolResult(
                    ToolInvocation.Status.PENDING_APPROVAL,
                    f"Approval required before running {name} (approval {approval.id}).",
                    invocation,
                    approval,
                )
            if approval.status in {ToolApproval.Status.REJECTED, ToolApproval.Status.EXPIRED}:
                invocation = _record(**record, status=ToolInvocation.Status.REJECTED, approval=approval)
                note = f": {approval.decision_note}" if approval.decision_note else ""
                return ToolResult(
                    ToolInvocation.Status.REJECTED,
                    f"The {name} action was {approval.status} and was not performed{note}.",
                    invocation,
                    approval,
                )
            if approval.status == ToolApproval.Status.EXECUTED:
                invocation = _record(
                    **record,
                    status=ToolInvocation.Status.DENIED,
                    deny_code="APPROVAL_ALREADY_USED",
                    approval=approval,
                )
                return ToolResult(
                    ToolInvocation.Status.DENIED, "Denied: this approval was already used.", invocation
                )
            # Approved: consume it before running so it can never execute twice.
            approval.status = ToolApproval.Status.EXECUTED
            approval.executed_at = timezone.now()
            approval.save(update_fields=["status", "executed_at"])

    ctx = ToolContext(
        user=user,
        organization_id=organization_id,
        config=dict(policy.config) if policy is not None else {},
        trace_id=trace_id,
    )
    started = time.monotonic()
    try:
        output = _run(spec, ctx, arguments)
    except (ToolDenied, PermissionError) as denial:
        code = getattr(denial, "code", "EGRESS_DENIED")
        invocation = _record(
            **record,
            status=ToolInvocation.Status.DENIED,
            deny_code=code,
            error=str(denial),
            approval=approval,
        )
        return ToolResult(ToolInvocation.Status.DENIED, f"Denied ({code}): {denial}", invocation, approval)
    except Exception as exc:  # noqa: BLE001 - tool failures are reported, never raised into the agent
        sentry_sdk.capture_exception(exc)
        invocation = _record(
            **record,
            status=ToolInvocation.Status.FAILED,
            error=str(exc),
            approval=approval,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return ToolResult(ToolInvocation.Status.FAILED, f"Tool {name} failed: {exc}", invocation, approval)
    output = output[: settings.TOOL_MAX_OUTPUT_CHARS]
    invocation = _record(
        **record,
        status=ToolInvocation.Status.SUCCEEDED,
        output=output,
        approval=approval,
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return ToolResult(ToolInvocation.Status.SUCCEEDED, output, invocation, approval)
