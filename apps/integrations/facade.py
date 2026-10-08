"""The frontend's ``/integrations/`` contract over :class:`ConnectorAccount`.

Supported providers are the ones the n8n ``knowledge-integration-sync`` and
``integration-test`` workflows implement. Provider credentials live in n8n
(platform service accounts / apps); a tenant connects by telling JT-Code
*which* folder, workspace, repository or channel to read and sharing it with
that service account. Connecting verifies access through n8n immediately.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from rest_framework.exceptions import ValidationError

from apps.integrations.models import Connector, ConnectorAccount

_SAFE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")
PROVIDERS: dict[str, dict[str, Any]] = {
    "github": {
        "name": "GitHub",
        "category": Connector.Category.DEVELOPMENT,
        "required": ("owner", "repo"),
        "optional": ("branch", "path"),
        "permissions": ["Read repository contents"],
    },
    "google_drive": {
        "name": "Google Drive",
        "category": Connector.Category.STORAGE,
        "required": ("folderId",),
        "optional": ("folderName",),
        "permissions": ["Read files in the shared folder"],
    },
    "notion": {
        "name": "Notion",
        "category": Connector.Category.PROJECT_MANAGEMENT,
        "required": (),
        "optional": ("workspaceName",),
        "permissions": ["Read pages shared with the JT-Code integration"],
    },
    "slack": {
        "name": "Slack",
        "category": Connector.Category.COMMUNICATION,
        "required": ("channelId",),
        "optional": ("channelName",),
        "permissions": ["Read channel history"],
    },
}
_PATTERNS = {
    "owner": _SAFE,
    "repo": _SAFE,
    "branch": re.compile(r"^[A-Za-z0-9_./\-]{1,200}$"),
    "path": re.compile(r"^[A-Za-z0-9_./\- ]{0,300}$"),
    "folderId": re.compile(r"^[A-Za-z0-9_\-]{10,200}$"),
    "channelId": re.compile(r"^[CGD][A-Z0-9]{6,20}$"),
}


def validate_config(key: str, config: Any) -> dict[str, str]:
    provider = PROVIDERS.get(key)
    if provider is None:
        raise ValidationError({"key": f"Unsupported integration. Choose one of {sorted(PROVIDERS)}."})
    if not isinstance(config, dict):
        raise ValidationError({"config": "config must be an object."})
    allowed = (*provider["required"], *provider["optional"])
    unknown = sorted(set(config) - set(allowed))
    if unknown:
        raise ValidationError({"config": f"Unknown fields for {key}: {unknown}. Allowed: {list(allowed)}."})
    cleaned: dict[str, str] = {}
    for name in allowed:
        value = config.get(name)
        if value in (None, ""):
            if name in provider["required"]:
                raise ValidationError({"config": f"{name} is required for {key}."})
            continue
        value = str(value).strip()
        pattern = _PATTERNS.get(name)
        if len(value) > 300 or (pattern and not pattern.fullmatch(value)) or ".." in value:
            raise ValidationError({"config": f"{name} has an invalid value."})
        cleaned[name] = value
    return cleaned


def connector_for(key: str) -> Connector:
    provider = PROVIDERS[key]
    connector, _ = Connector.objects.get_or_create(
        slug=key,
        defaults={
            "name": provider["name"],
            "category": provider["category"],
            "auth_type": Connector.AuthType.OAUTH2,
            "is_active": True,
            "is_verified": True,
            "supported_operations": ["knowledge_sync", "connection_test"],
        },
    )
    return connector


def public_config(account: ConnectorAccount) -> dict[str, Any]:
    return dict((account.metadata or {}).get("config") or {})


def is_syncing(account: ConnectorAccount) -> bool:
    from apps.orchestration.deliveries import open_deliveries_for
    from apps.orchestration.knowledge import SYNC_EVENT

    return bool(open_deliveries_for(SYNC_EVENT).filter(payload__integration__id=str(account.id)).exists())


def serialize(account: ConnectorAccount) -> dict[str, Any]:
    key = account.connector.slug
    connected = account.status == ConnectorAccount.Status.ACTIVE
    if connected and is_syncing(account):
        status = "syncing"
    elif connected:
        status = "connected"
    elif account.status in (ConnectorAccount.Status.ERROR, ConnectorAccount.Status.EXPIRED):
        status = "error"
    else:
        status = "disconnected"
    return {
        "id": str(account.id),
        "key": key,
        "name": PROVIDERS.get(key, {}).get("name", account.connector.name),
        "displayName": account.name,
        "connected": connected,
        "status": status,
        "lastSync": account.last_sync_at.isoformat() if account.last_sync_at else None,
        "permissions": list(PROVIDERS.get(key, {}).get("permissions", [])),
        "config": public_config(account),
        "lastError": account.last_error,
    }


def check_connection(account: ConnectorAccount) -> tuple[bool, str]:
    """Ask n8n (``integration-test`` workflow) whether it can read the tenant's resource."""
    from apps.orchestration import client
    from apps.orchestration.registry import active_definition

    definition = active_definition("integration-test")
    if definition is None or not client.configured():
        return False, "n8n is not configured, so the connection cannot be verified."
    payload = {
        "kind": "request",
        "integration": {
            "id": str(account.id),
            "key": account.connector.slug,
            "config": public_config(account),
        },
    }
    try:
        response = client.post_signed(
            definition.webhook_path, payload, idempotency_key=f"test:{uuid.uuid4()}"
        )
    except client.N8nError as exc:
        return False, f"Connection check failed: {exc}"
    body = client.response_json(response)
    return bool(body.get("ok")), str(body.get("message") or "No result from n8n.")[:500]


def apply_check(account: ConnectorAccount) -> tuple[bool, str]:
    ok, message = check_connection(account)
    account.status = ConnectorAccount.Status.ACTIVE if ok else ConnectorAccount.Status.ERROR
    account.last_error = "" if ok else message
    account.save(update_fields=["status", "last_error", "updated_at"])
    return ok, message
