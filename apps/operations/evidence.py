"""Record verification runs (``with recorded(kind, ...) as run:``)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.operations.models import VerificationRun


def environment_name() -> str:
    return str(getattr(settings, "SENTRY_ENVIRONMENT", "") or os.environ.get("DJANGO_SETTINGS_MODULE", ""))


class RunHandle:
    def __init__(self, run: VerificationRun) -> None:
        self.run = run
        self.failures: list[str] = []
        self.summary: dict[str, Any] = {}

    def fail(self, message: str) -> None:
        self.failures.append(message)


def _plain(parameters: dict[str, Any] | None) -> dict[str, Any]:
    """Keep JSON-safe option values only (``call_command`` passes streams such as ``stdout``)."""
    return {
        key: value
        for key, value in (parameters or {}).items()
        if isinstance(value, str | int | float | bool | type(None) | list | tuple)
    }


@contextmanager
def recorded(kind: str, parameters: dict[str, Any] | None = None) -> Iterator[RunHandle]:
    """Persist a run; it passes unless the block raises or calls ``handle.fail``."""
    run = VerificationRun.objects.create(
        kind=kind,
        environment=environment_name(),
        git_sha=os.environ.get("GIT_SHA", "") or os.environ.get("GITHUB_SHA", ""),
        parameters=_plain(parameters),
    )
    handle = RunHandle(run)
    try:
        yield handle
    except Exception as exc:
        handle.fail(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        run.summary = handle.summary
        run.failures = handle.failures
        run.status = VerificationRun.Status.FAILED if handle.failures else VerificationRun.Status.PASSED
        run.finished_at = timezone.now()
        run.save(update_fields=["summary", "failures", "status", "finished_at"])


def record_result(
    kind: str, *, passed: bool, summary: dict[str, Any], failures: list[str] | None = None
) -> None:
    """Store a completed run in one step (for commands that compute their verdict first)."""
    VerificationRun.objects.create(
        kind=kind,
        status=VerificationRun.Status.PASSED if passed else VerificationRun.Status.FAILED,
        environment=environment_name(),
        git_sha=os.environ.get("GIT_SHA", "") or os.environ.get("GITHUB_SHA", ""),
        summary=summary,
        failures=failures or [],
        finished_at=timezone.now(),
    )
