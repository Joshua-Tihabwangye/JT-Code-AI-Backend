"""Run the analytics engine in an isolated, resource-limited child process.

The child is ``python -I apps/analytics/engine.py`` (isolated mode: no
``PYTHONPATH``, user site or script-directory imports) started with an empty
environment except for locale, single-threaded math libraries and a private
temporary ``HOME``/Matplotlib cache. It therefore holds no database, Supabase Storage,
model-provider or Django credentials. The engine applies address-space, CPU,
file-size and open-file limits before reading the request, and the parent
enforces a wall-clock timeout.
"""

from __future__ import annotations

import base64
import json
import subprocess  # nosec B404 - fixed argv, no shell, isolated interpreter
import sys
import tempfile
from pathlib import Path
from typing import Any

from django.conf import settings

ENGINE_PATH = Path(__file__).with_name("engine.py")


class SandboxError(RuntimeError):
    """The isolated worker crashed or exceeded a resource limit."""


def _limits() -> dict[str, Any]:
    return {
        "memory_bytes": settings.ANALYTICS_SANDBOX_MEMORY_MB * 1024 * 1024,
        "cpu_seconds": settings.ANALYTICS_SANDBOX_CPU_SECONDS,
        "file_bytes": 64 * 1024 * 1024,
        "max_bytes": settings.ANALYTICS_MAX_DATASET_BYTES,
        "max_rows": settings.ANALYTICS_MAX_DATASET_ROWS,
        "max_columns": settings.ANALYTICS_MAX_DATASET_COLUMNS,
        "max_cells": settings.ANALYTICS_MAX_DATASET_CELLS,
        "max_result_bytes": settings.ANALYTICS_MAX_RESULT_BYTES,
        "max_points": settings.ANALYTICS_MAX_CHART_POINTS,
        "max_spec_bytes": settings.ANALYTICS_MAX_PLOTLY_SPEC_BYTES,
        "preview_rows": settings.ANALYTICS_RESULT_PREVIEW_ROWS,
        "allowed_mime_types": list(settings.ANALYTICS_ALLOWED_MIME_TYPES),
    }


def run_engine(operation: str, content: bytes, **request: Any) -> dict[str, Any]:
    """Execute one engine operation; raises ``AnalysisError`` or ``SandboxError``."""
    from apps.analytics.services import AnalysisError

    limits = _limits()
    payload = json.dumps(
        {"op": operation, "data": base64.b64encode(content).decode("ascii"), "limits": limits, **request}
    ).encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="jt-analytics-") as workdir:
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": workdir,
            "TMPDIR": workdir,
            "MPLCONFIGDIR": workdir,
            "MPLBACKEND": "Agg",
            "LANG": "C.UTF-8",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "JT_ANALYTICS_LIMITS": json.dumps(limits),
        }
        try:
            completed = subprocess.run(  # nosec B603 - fixed interpreter and engine path
                [sys.executable, "-I", str(ENGINE_PATH)],
                input=payload,
                capture_output=True,
                env=environment,
                cwd=workdir,
                timeout=settings.ANALYTICS_SANDBOX_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AnalysisError("The analysis exceeded its time limit.") from exc
    if completed.returncode != 0 or not completed.stdout:
        if completed.returncode < 0:
            raise AnalysisError("The analysis exceeded its resource limits and was stopped.")
        raise SandboxError(f"Analytics engine exited with status {completed.returncode}.")
    try:
        response = json.loads(completed.stdout)
    except ValueError as exc:
        raise SandboxError("Analytics engine returned an invalid response.") from exc
    if response.get("ok"):
        return dict(response)
    if response.get("kind") in {"analysis", "resources"}:
        raise AnalysisError(str(response.get("error") or "The analysis failed."))
    raise SandboxError(f"Analytics engine failed: {response.get('error')}")
