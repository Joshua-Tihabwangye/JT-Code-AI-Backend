"""Sentry initialization with PII scrubbing (Phase 15).

Nothing that identifies a person or authenticates a request may leave the
process: request bodies, cookies and query strings are never sent, headers are
reduced to an allowlist, users are reduced to their opaque id, and every string
in the event (messages, exception values, breadcrumbs, tags, contexts and
extras) is scrubbed for e-mail addresses, bearer tokens, JWTs, API keys and
card numbers. Values under sensitive keys are replaced outright.
"""

from __future__ import annotations

import re
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.redis import RedisIntegration
from sentry_sdk.scrubber import DEFAULT_DENYLIST, DEFAULT_PII_DENYLIST, EventScrubber

FILTERED = "[Filtered]"
MAX_DEPTH = 8

SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|authorization|auth|cookie|session|signature|"
    r"credential|private|dsn|card|cvc|cvv|iban|ssn|email|e-mail|phone|address|ip_address|"
    r"client_secret|refresh|otp|jwt|bearer)",
    re.IGNORECASE,
)
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer " + FILTERED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"), FILTERED),
    (
        re.compile(
            r"\b(?:sk|rk|pk|whsec|sb_secret|sb_publishable|jtk_live|jtk_test|ghp|gho|xox[abp])_"
            r"[A-Za-z0-9_-]{6,}"
        ),
        FILTERED,
    ),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
    (re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), "[number]"),
    (re.compile(r"(?i)\b(password|secret|token|api_key|apikey|key)=([^&\s]+)"), r"\1=" + FILTERED),
)
# Request headers that are safe and useful for debugging; everything else is dropped.
SAFE_HEADERS = frozenset(
    {
        "accept",
        "accept-language",
        "content-type",
        "content-length",
        "user-agent",
        "x-request-id",
        "x-trace-id",
        "traceparent",
        "x-organization-id",
        "host",
    }
)
_IGNORED_TRANSACTIONS = ("/api/v1/health/", "/metrics")


def scrub_text(value: str) -> str:
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def scrub(value: Any, depth: int = 0) -> Any:
    """Recursively scrub strings and replace values stored under sensitive keys."""
    if depth > MAX_DEPTH:
        return FILTERED
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {
            key: FILTERED if isinstance(key, str) and SENSITIVE_KEY.search(key) else scrub(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [scrub(item, depth + 1) for item in value]
    return value


def _scrub_request(request: dict[str, Any]) -> dict[str, Any]:
    headers = request.get("headers") or {}
    cleaned: dict[str, Any] = {
        "method": request.get("method"),
        "url": scrub_text(str(request.get("url") or "")).split("?", 1)[0],
        "headers": {
            key: scrub_text(str(value)) for key, value in headers.items() if str(key).lower() in SAFE_HEADERS
        },
    }
    return {key: value for key, value in cleaned.items() if value}


def _scrub_exception(exception: dict[str, Any]) -> None:
    for item in exception.get("values") or []:
        if isinstance(item.get("value"), str):
            item["value"] = scrub_text(item["value"])
        for frame in (item.get("stacktrace") or {}).get("frames") or []:
            frame.pop("vars", None)


def before_send(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if "request" in event:
        event["request"] = _scrub_request(event["request"])
    if user := event.get("user"):
        event["user"] = {"id": user["id"]} if user.get("id") else {}
    if "exception" in event:
        _scrub_exception(event["exception"])
    if isinstance(logentry := event.get("logentry"), dict):
        if isinstance(logentry.get("message"), str):
            logentry["message"] = scrub_text(logentry["message"])
        if "formatted" in logentry:
            logentry["formatted"] = scrub_text(str(logentry["formatted"]))
        if "params" in logentry:
            logentry["params"] = scrub(logentry["params"])
    if isinstance(event.get("message"), str):
        event["message"] = scrub_text(event["message"])
    for key in ("extra", "contexts", "tags"):
        if key in event:
            event[key] = scrub(event[key])
    breadcrumbs = event.get("breadcrumbs")
    if isinstance(breadcrumbs, dict) and isinstance(breadcrumbs.get("values"), list):
        breadcrumbs["values"] = [crumb for crumb in map(before_breadcrumb, breadcrumbs["values"]) if crumb]
    return event


def before_send_transaction(
    event: dict[str, Any], hint: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    if str(event.get("transaction") or "").startswith(_IGNORED_TRANSACTIONS):
        return None
    return before_send(event, hint)


def before_breadcrumb(crumb: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if isinstance(crumb.get("message"), str):
        crumb["message"] = scrub_text(crumb["message"])
    data = crumb.get("data")
    if isinstance(data, dict):
        if isinstance(data.get("url"), str):
            data["url"] = scrub_text(data["url"]).split("?", 1)[0]
        data.pop("http.query", None)
        crumb["data"] = scrub(data)
    return crumb


def traces_sampler(context: dict[str, Any]) -> float:
    from django.conf import settings

    path = str((context.get("wsgi_environ") or {}).get("PATH_INFO") or "")
    if path.startswith(_IGNORED_TRANSACTIONS):
        return 0.0
    return float(getattr(settings, "SENTRY_TRACES_SAMPLE_RATE", 0.0))


def init_sentry(
    *, dsn: str, environment: str, release: str, traces_sample_rate: float, profiles_sample_rate: float
) -> None:
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        integrations=[DjangoIntegration(), CeleryIntegration(), RedisIntegration()],
        traces_sample_rate=traces_sample_rate,
        traces_sampler=traces_sampler,
        profiles_sample_rate=profiles_sample_rate,
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        event_scrubber=EventScrubber(
            denylist=[*DEFAULT_DENYLIST, "stripe_signature", "x_jt_code_signature", "client_secret"],
            pii_denylist=[*DEFAULT_PII_DENYLIST, "email"],
            recursive=True,
        ),
        before_send=before_send,  # type: ignore[arg-type]
        before_send_transaction=before_send_transaction,  # type: ignore[arg-type]
        before_breadcrumb=before_breadcrumb,
    )
