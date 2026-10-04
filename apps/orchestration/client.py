"""HTTP access to n8n: signed webhook dispatch and the public admin API.

Django -> n8n webhook calls are signed with ``N8N_DISPATCH_SECRET`` using the
timestamp/nonce scheme of :mod:`apps.core.signing` (the workflows'
"Verify JT-Code signature" node checks it) and carry ``traceparent`` so the
n8n execution joins the caller's trace. Errors are classified: network
failures, timeouts, 404 (webhook not registered yet), 408/425/429 and 5xx are
retryable; other 4xx responses are permanent.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from django.conf import settings

from apps.core.signing import sign_request
from apps.core.tracing import inject_headers, tracer

_RETRYABLE_STATUS = frozenset({404, 408, 425, 429})


class N8nError(Exception):
    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class N8nNotConfigured(N8nError):
    def __init__(self) -> None:
        super().__init__("n8n is not configured.", retryable=False)


def configured() -> bool:
    return bool(webhook_base_url() and settings.N8N_DISPATCH_SECRET and settings.N8N_CALLBACK_BASE_URL)


def webhook_base_url() -> str:
    return str(settings.N8N_WEBHOOK_BASE_URL or settings.N8N_BASE_URL or "").rstrip("/")


def webhook_url(path: str) -> str:
    return f"{webhook_base_url()}/webhook/{path.lstrip('/')}"


def callback_url(path: str) -> str:
    return f"{str(settings.N8N_CALLBACK_BASE_URL).rstrip('/')}/{path.lstrip('/')}"


def http_client(**kwargs: Any) -> httpx.Client:
    """Factory (patched in tests) for every outbound n8n request."""
    return httpx.Client(timeout=settings.N8N_REQUEST_TIMEOUT_SECONDS, follow_redirects=False, **kwargs)


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode()


def post_signed(path: str, payload: dict[str, Any], *, idempotency_key: str) -> httpx.Response:
    """Sign and POST ``payload`` to the n8n webhook ``path``; raise :class:`N8nError`."""
    if not configured():
        raise N8nNotConfigured()
    body = encode(payload)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "JT-Code-Orchestrator/1.0",
        "Idempotency-Key": idempotency_key,
        **sign_request(body, settings.N8N_DISPATCH_SECRET),
    }
    with tracer().start_as_current_span(f"n8n POST {path}", attributes={"n8n.webhook.path": path}):
        inject_headers(headers)
        try:
            with http_client() as client:
                response = client.post(webhook_url(path), content=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise N8nError(f"n8n timed out: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise N8nError(f"n8n is unreachable: {exc}", retryable=True) from exc
    if response.status_code >= 500 or response.status_code in _RETRYABLE_STATUS:
        raise N8nError(f"n8n returned {response.status_code}", retryable=True, status=response.status_code)
    if response.status_code >= 400:
        raise N8nError(
            f"n8n rejected the request ({response.status_code}): {response.text[:300]}",
            retryable=False,
            status=response.status_code,
        )
    return response


def response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class N8nAdmin:
    """The n8n public REST API (``/api/v1``), authenticated with ``N8N_API_KEY``."""

    def __init__(self) -> None:
        if not settings.N8N_BASE_URL or not settings.N8N_API_KEY:
            raise N8nNotConfigured()
        self.base = f"{str(settings.N8N_BASE_URL).rstrip('/')}/api/v1"
        self.headers = {"X-N8N-API-KEY": settings.N8N_API_KEY, "Accept": "application/json"}

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            with http_client() as client:
                response = client.request(method, f"{self.base}{path}", headers=self.headers, **kwargs)
        except httpx.HTTPError as exc:
            raise N8nError(f"n8n API is unreachable: {exc}", retryable=True) from exc
        if response.status_code >= 400:
            raise N8nError(
                f"n8n API {method} {path} failed ({response.status_code}): {response.text[:300]}",
                retryable=response.status_code >= 500,
                status=response.status_code,
            )
        return response_json(response)

    def list_workflows(self) -> list[dict[str, Any]]:
        workflows: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
            page = self._request("GET", "/workflows", params=params)
            workflows.extend(page.get("data") or [])
            cursor = page.get("nextCursor")
            if not cursor:
                return workflows

    def create_workflow(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/workflows", json=body)

    def update_workflow(self, workflow_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", f"/workflows/{workflow_id}", json=body)

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workflows/{workflow_id}")

    def activate(self, workflow_id: str) -> dict[str, Any]:
        return self._request("POST", f"/workflows/{workflow_id}/activate")

    def deactivate(self, workflow_id: str) -> dict[str, Any]:
        return self._request("POST", f"/workflows/{workflow_id}/deactivate")

    def get_execution(self, execution_id: str) -> dict[str, Any]:
        return self._request("GET", f"/executions/{execution_id}")
