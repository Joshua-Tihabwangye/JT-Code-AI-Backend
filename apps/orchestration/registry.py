"""Versioned n8n workflow definitions (``n8n/workflows/<key>.v<version>.json``).

The JSON files are the source of truth: an n8n export plus a
``meta.jtCode`` block that tells Django what the workflow is for. A version is
immutable - changing a registered file without bumping its version is an
error - and the highest version of each key is the one Django dispatches to.

Validation enforces the integration contract on every definition:

* the file name, workflow name and webhook path agree with the key/version;
* job/event/request workflows start by verifying the JT-Code signature and
  report back only through signed callbacks;
* credentials are referenced through ``${N8N_CREDENTIAL_*}`` placeholders,
  never by instance-specific ids;
* successful executions are not stored in n8n (``saveDataSuccessExecution``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from apps.events.contracts import EventContractError, validate_event_type

KINDS = ("job", "event", "request", "error")
_FILE = re.compile(r"^(?P<key>[a-z0-9][a-z0-9-]*)\.v(?P<version>[1-9][0-9]*)\.json$")
_PLACEHOLDER = re.compile(r"^\$\{(N8N_CREDENTIAL_[A-Z0-9_]+|JT_CODE_ERROR_WORKFLOW_ID)\}$")
_CACHE_KEY = "orchestration:routing:v1"
VERIFY_NODE = "Verify JT-Code signature"


class RegistryError(ValueError):
    """A workflow definition breaks the contract or changed without a version bump."""


@dataclass(frozen=True)
class WorkflowSpec:
    key: str
    version: int
    kind: str
    name: str
    description: str
    webhook_path: str
    task_types: tuple[str, ...]
    event_types: tuple[str, ...]
    timeout_seconds: int
    max_attempts: int
    required_credentials: tuple[str, ...]
    definition: dict[str, Any] = field(repr=False)
    checksum: str = ""


def canonical_checksum(definition: dict[str, Any]) -> str:
    payload = {key: definition.get(key) for key in ("name", "nodes", "connections", "settings")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def workflows_dir() -> Path:
    return Path(getattr(settings, "N8N_WORKFLOWS_DIR", "") or Path(settings.BASE_DIR) / "n8n" / "workflows")


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _walk_strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _walk_strings(item)]
    return []


def validate_definition(path: Path, data: dict[str, Any]) -> WorkflowSpec:
    match = _FILE.fullmatch(path.name)
    if match is None:
        raise RegistryError(f"{path.name}: files must be named <key>.v<version>.json")
    meta = (data.get("meta") or {}).get("jtCode")
    if not isinstance(meta, dict):
        raise RegistryError(f"{path.name}: meta.jtCode is required")
    key, version = meta.get("key"), meta.get("version")
    if key != match["key"] or version != int(match["version"]):
        raise RegistryError(f"{path.name}: meta.jtCode key/version must match the file name")
    kind = meta.get("kind")
    if kind not in KINDS:
        raise RegistryError(f"{path.name}: kind must be one of {KINDS}")
    expected_name = f"{settings.N8N_WORKFLOW_PREFIX}.{key}.v{version}"
    if data.get("name") != expected_name:
        raise RegistryError(f"{path.name}: workflow name must be {expected_name!r}")
    nodes = data.get("nodes") or []
    names = {node.get("name") for node in nodes}
    if len(names) != len(nodes):
        raise RegistryError(f"{path.name}: node names must be unique")
    for source, outputs in (data.get("connections") or {}).items():
        targets = [t["node"] for branch in outputs.get("main", []) for t in branch]
        if source not in names or any(target not in names for target in targets):
            raise RegistryError(f"{path.name}: connections reference unknown nodes")
    webhook_path = str(meta.get("webhookPath") or "")
    hooks = [n for n in nodes if n.get("type") == "n8n-nodes-base.webhook"]
    if kind == "error":
        if not any(n.get("type") == "n8n-nodes-base.errorTrigger" for n in nodes):
            raise RegistryError(f"{path.name}: error workflows start with an Error Trigger")
    else:
        if webhook_path != f"{settings.N8N_WORKFLOW_PREFIX}/{key}/v{version}":
            raise RegistryError(f"{path.name}: webhookPath must be jt-code/<key>/v<version>")
        if len(hooks) != 1 or hooks[0]["parameters"].get("path") != webhook_path:
            raise RegistryError(f"{path.name}: exactly one webhook node must listen on webhookPath")
        if hooks[0]["parameters"].get("responseMode") != "responseNode":
            raise RegistryError(
                f"{path.name}: the webhook must answer from a Respond node (401 on bad signature)"
            )
        if VERIFY_NODE not in names:
            raise RegistryError(f"{path.name}: the {VERIFY_NODE!r} node is required")
    task_types = tuple(meta.get("taskTypes") or ())
    event_types = tuple(meta.get("eventTypes") or ())
    if kind == "job" and not task_types:
        raise RegistryError(f"{path.name}: job workflows declare taskTypes")
    if kind == "event" and not event_types:
        raise RegistryError(f"{path.name}: event workflows declare eventTypes")
    for event_type in event_types:
        try:
            validate_event_type(event_type)
        except EventContractError as exc:
            raise RegistryError(f"{path.name}: {exc}") from exc
        if event_type.startswith("orchestration."):
            raise RegistryError(
                f"{path.name}: workflows may not subscribe to orchestration.* (feedback loop)"
            )
    for node in nodes:
        for credential in (node.get("credentials") or {}).values():
            if not _PLACEHOLDER.fullmatch(str(credential.get("id", ""))):
                raise RegistryError(
                    f"{path.name}: node {node.get('name')!r} must reference credentials "
                    "as ${N8N_CREDENTIAL_*}"
                )
    if (data.get("settings") or {}).get("saveDataSuccessExecution") != "none":
        raise RegistryError(f"{path.name}: settings.saveDataSuccessExecution must be 'none'")
    posts_to_jt_code = any(
        "X-JT-Code-Signature" in _walk_strings(n.get("parameters"))
        for n in nodes
        if "httpRequest" in n["type"]
    )
    if kind in {"job", "event", "error"} and not posts_to_jt_code:
        raise RegistryError(f"{path.name}: callbacks to JT-Code must be signed (X-JT-Code-Signature)")
    definition = {key: data[key] for key in ("name", "nodes", "connections", "settings") if key in data}
    definition["staticData"] = data.get("staticData")
    return WorkflowSpec(
        key=key,
        version=version,
        kind=kind,
        name=data["name"],
        description=str(meta.get("description") or ""),
        webhook_path=webhook_path,
        task_types=task_types,
        event_types=event_types,
        timeout_seconds=int(meta.get("timeoutSeconds") or 1800),
        max_attempts=max(1, int(meta.get("maxAttempts") or 5)),
        required_credentials=tuple(meta.get("requiredCredentials") or ()),
        definition=definition,
        checksum=canonical_checksum(definition),
    )


def load_specs(directory: Path | None = None) -> list[WorkflowSpec]:
    directory = directory or workflows_dir()
    specs = [
        validate_definition(path, json.loads(path.read_text())) for path in sorted(directory.glob("*.json"))
    ]
    claimed: dict[str, str] = {}
    latest: dict[str, int] = {}
    for spec in specs:
        latest[spec.key] = max(latest.get(spec.key, 0), spec.version)
    for spec in specs:
        if spec.version != latest[spec.key]:
            continue
        for task_type in spec.task_types:
            if task_type in claimed:
                raise RegistryError(f"{task_type} is claimed by both {claimed[task_type]} and {spec.key}")
            claimed[task_type] = spec.key
    return specs


@transaction.atomic
def register_specs(specs: list[WorkflowSpec]) -> list[str]:
    """Upsert definitions; activate the newest version of each key. Returns change notes."""
    from apps.orchestration.models import WorkflowDefinition

    notes: list[str] = []
    newest: dict[str, int] = {}
    for spec in specs:
        newest[spec.key] = max(newest.get(spec.key, 0), spec.version)
        row = WorkflowDefinition.objects.filter(key=spec.key, version=spec.version).first()
        if row is not None:
            if row.checksum != spec.checksum:
                raise RegistryError(
                    f"{spec.key} v{spec.version} changed after registration; bump the version instead."
                )
            continue
        WorkflowDefinition.objects.create(
            key=spec.key,
            version=spec.version,
            name=spec.name,
            description=spec.description,
            kind=spec.kind,
            definition=spec.definition,
            checksum=spec.checksum,
            webhook_path=spec.webhook_path,
            task_types=list(spec.task_types),
            event_types=list(spec.event_types),
            timeout_seconds=spec.timeout_seconds,
            max_attempts=spec.max_attempts,
            required_credentials=list(spec.required_credentials),
            is_active=False,
        )
        notes.append(f"registered {spec.key} v{spec.version}")
    for key, version in newest.items():
        stale = WorkflowDefinition.objects.filter(key=key, is_active=True).exclude(version=version)
        if stale.exists():
            stale.update(is_active=False)
            notes.append(f"retired older versions of {key}")
        if WorkflowDefinition.objects.filter(key=key, version=version, is_active=False).update(
            is_active=True
        ):
            notes.append(f"activated {key} v{version}")
    transaction.on_commit(invalidate_routing)
    return notes


def register_after_migrate(sender: Any = None, **kwargs: Any) -> None:
    using = kwargs.get("using", "default")
    if using != "default":
        return
    register_specs(load_specs())


# Routing lookups (cached; invalidated on registration) ------------------------


def invalidate_routing() -> None:
    # The cache is an optimisation; an unreachable Redis must not fail migrate or registration.
    with contextlib.suppress(Exception):
        cache.delete(_CACHE_KEY)


def _routing() -> dict[str, Any]:
    cached = None
    with contextlib.suppress(Exception):
        cached = cache.get(_CACHE_KEY)
    if cached is not None:
        return dict(cached)
    from apps.orchestration.models import WorkflowDefinition

    tasks: dict[str, str] = {}
    events: dict[str, list[str]] = {}
    for row in WorkflowDefinition.objects.filter(is_active=True).only("id", "task_types", "event_types"):
        for task_type in row.task_types:
            tasks[task_type] = str(row.id)
        for event_type in row.event_types:
            events.setdefault(event_type, []).append(str(row.id))
    routing = {"tasks": tasks, "events": events}
    with contextlib.suppress(Exception):
        cache.set(_CACHE_KEY, routing, timeout=60)
    return routing


def n8n_task_types() -> set[str]:
    return set(_routing()["tasks"])


def definition_for_task(task_type: str) -> Any:
    from apps.orchestration.models import WorkflowDefinition

    definition_id = _routing()["tasks"].get(task_type)
    return WorkflowDefinition.objects.filter(id=definition_id).first() if definition_id else None


def definitions_for_event(event_type: str) -> list[str]:
    return list(_routing()["events"].get(event_type, []))


def active_definition(key: str) -> Any:
    from apps.orchestration.models import WorkflowDefinition

    return WorkflowDefinition.objects.filter(key=key, is_active=True).first()
