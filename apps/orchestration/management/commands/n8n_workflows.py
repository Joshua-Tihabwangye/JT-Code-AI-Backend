"""Manage the versioned n8n workflow definitions.

    python manage.py n8n_workflows validate   # contract checks on n8n/workflows/*.json (no DB, no n8n)
    python manage.py n8n_workflows register   # record new versions in Django; activate the newest
    python manage.py n8n_workflows push       # register, then create/update and (de)activate in n8n
    python manage.py n8n_workflows check      # fail if n8n drifted from the registered definitions

``push`` substitutes ``${N8N_CREDENTIAL_*}`` with the credential ids from
settings (``N8N_CREDENTIAL_IDS``) and ``${JT_CODE_ERROR_WORKFLOW_ID}`` with the
deployed error workflow, deploys the error workflow first, activates only the
current version of each key and deactivates superseded versions. A workflow
whose credentials are not configured is deployed inactive and reported.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.orchestration import client
from apps.orchestration.models import WorkflowDefinition
from apps.orchestration.registry import RegistryError, load_specs, register_specs

_API_FIELDS = ("name", "nodes", "connections", "settings", "staticData")


def substitute(definition: dict[str, Any], values: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """Replace ``${NAME}`` placeholders; return the body and unresolved names."""
    text = json.dumps(definition)
    missing = []
    for name in sorted(set(_placeholders(text))):
        if values.get(name):
            text = text.replace("${" + name + "}", values[name])
        else:
            missing.append(name)
    return json.loads(text), missing


def _placeholders(text: str) -> list[str]:
    import re

    return re.findall(r"\$\{((?:N8N_CREDENTIAL|JT_CODE)_[A-Z0-9_]+)\}", text)


def api_body(definition: dict[str, Any]) -> dict[str, Any]:
    body = {key: copy.deepcopy(definition.get(key)) for key in _API_FIELDS if key in definition}
    body["staticData"] = body.get("staticData") or None
    if not body["settings"].get("errorWorkflow"):
        body["settings"].pop("errorWorkflow", None)
    return body


def projection(definition: dict[str, Any], *, settings_keys: Any = None) -> dict[str, Any]:
    """What must match between Django and n8n (n8n adds defaults we do not compare)."""
    keys = list(settings_keys or (definition.get("settings") or {}))
    nodes = sorted(
        (n.get("name"), n.get("type"), n.get("typeVersion"), json.dumps(n.get("parameters"), sort_keys=True))
        for n in definition.get("nodes") or []
    )
    return {
        "name": definition.get("name"),
        "nodes": nodes,
        "connections": json.dumps(definition.get("connections"), sort_keys=True),
        "settings": {
            key: (definition.get("settings") or {}).get(key) for key in keys if key != "errorWorkflow"
        },
    }


class Command(BaseCommand):
    help = "Validate, register, deploy and drift-check the versioned n8n workflows."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("action", choices=("validate", "register", "push", "check"))

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            specs = load_specs()
        except RegistryError as exc:
            raise CommandError(str(exc)) from exc
        action = options["action"]
        if action == "validate":
            for spec in specs:
                self.stdout.write(f"ok  {spec.key} v{spec.version} ({spec.kind})")
            return
        try:
            notes = register_specs(specs)
        except RegistryError as exc:
            raise CommandError(str(exc)) from exc
        for note in notes:
            self.stdout.write(note)
        if action == "push":
            self.push()
        elif action == "check":
            self.check_drift()

    def push(self) -> None:
        admin = client.N8nAdmin()
        remote = {item.get("name"): item for item in admin.list_workflows()}
        values = {name: str(value) for name, value in (settings.N8N_CREDENTIAL_IDS or {}).items()}
        definitions = list(WorkflowDefinition.objects.order_by("key", "version"))
        definitions.sort(key=lambda d: d.kind != WorkflowDefinition.Kind.ERROR)  # error workflow first
        problems = []
        for definition in definitions:
            body, missing = substitute(definition.definition, values)
            body = api_body(body)
            existing = remote.get(definition.name)
            workflow = (
                admin.update_workflow(existing["id"], body) if existing else admin.create_workflow(body)
            )
            workflow_id = str(workflow.get("id") or (existing or {}).get("id") or "")
            definition.n8n_workflow_id = workflow_id
            definition.synced_checksum = definition.checksum
            definition.synced_at = timezone.now()
            definition.sync_error = ""
            should_run = definition.is_active and not missing
            if definition.kind == WorkflowDefinition.Kind.ERROR and definition.is_active:
                values["JT_CODE_ERROR_WORKFLOW_ID"] = workflow_id
            if missing:
                definition.sync_error = f"Unresolved placeholders: {', '.join(missing)}"
                problems.append(f"{definition.key} v{definition.version}: {definition.sync_error}")
            if should_run and definition.kind != WorkflowDefinition.Kind.ERROR:
                admin.activate(workflow_id)
            elif not should_run and (existing or {}).get("active"):
                admin.deactivate(workflow_id)
            definition.n8n_active = should_run and definition.kind != WorkflowDefinition.Kind.ERROR
            definition.save()
            self.stdout.write(
                f"{'active ' if definition.n8n_active else 'stored '} {definition.name} -> {workflow_id}"
            )
        if problems:
            raise CommandError("Deployed with problems:\n" + "\n".join(problems))

    def check_drift(self) -> None:
        admin = client.N8nAdmin()
        remote = {item.get("name"): item for item in admin.list_workflows()}
        values = {name: str(value) for name, value in (settings.N8N_CREDENTIAL_IDS or {}).items()}
        error = WorkflowDefinition.objects.filter(kind=WorkflowDefinition.Kind.ERROR, is_active=True).first()
        if error and error.n8n_workflow_id:
            values["JT_CODE_ERROR_WORKFLOW_ID"] = error.n8n_workflow_id
        drift = []
        for definition in WorkflowDefinition.objects.filter(is_active=True):
            current = remote.get(definition.name)
            if current is None:
                drift.append(f"{definition.name}: missing in n8n")
                continue
            expected, _ = substitute(definition.definition, values)
            detail = admin.get_workflow(str(current["id"]))
            if projection(api_body(expected)) != projection(detail, settings_keys=expected["settings"]):
                drift.append(f"{definition.name}: n8n copy differs from the registered version (edit + bump)")
            if definition.kind != WorkflowDefinition.Kind.ERROR and not detail.get("active"):
                drift.append(f"{definition.name}: not active in n8n")
        if drift:
            raise CommandError("n8n drifted from the registered workflows:\n" + "\n".join(drift))
        self.stdout.write(self.style.SUCCESS("n8n matches the registered workflow definitions."))
