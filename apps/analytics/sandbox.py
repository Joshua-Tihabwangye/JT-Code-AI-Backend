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
import itertools
import json
import os
import struct
import subprocess  # nosec B404 - fixed argv, no shell, isolated interpreter
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from django.conf import settings

ENGINE_PATH = Path(__file__).with_name("engine.py")
ZYGOTE_PATH = Path(__file__).with_name("zygote.py")


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
        "max_spec_bytes": settings.ANALYTICS_MAX_CHART_SPEC_BYTES,
        "preview_rows": settings.ANALYTICS_RESULT_PREVIEW_ROWS,
        "allowed_mime_types": list(settings.ANALYTICS_ALLOWED_MIME_TYPES),
    }


def _child_environment(workdir: str, limits: dict[str, Any]) -> dict[str, str]:
    return {
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


class _ZygoteUnavailable(RuntimeError):
    """The fork server could not run the request; the caller falls back to a fresh process."""


class _Zygote:
    """One pre-loaded fork server (apps/analytics/zygote.py) per engine script and server process.

    Requests are multiplexed by id over its stdin/stdout; a reader thread hands each response to the
    waiting caller. Each request still runs in its own forked, resource-limited, guarded child.
    """

    def __init__(self, script: Path) -> None:
        self._home = tempfile.mkdtemp(prefix="jt-zygote-")
        self._process = subprocess.Popen(  # nosec B603 - fixed interpreter, zygote and engine paths
            [sys.executable, "-I", str(ZYGOTE_PATH), str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_child_environment(self._home, {}),
            cwd=self._home,
        )
        self._ids = itertools.count(1)
        self._write_lock = threading.Lock()
        self._pending: dict[int, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self.alive = True
        threading.Thread(target=self._read_responses, name="jt-zygote-reader", daemon=True).start()

    def _read_exact(self, size: int) -> bytes | None:
        assert self._process.stdout is not None
        data = self._process.stdout.read(size)
        return data if data is not None and len(data) == size else None

    def _read_responses(self) -> None:
        while True:
            size = self._read_exact(4)
            header = size and self._read_exact(struct.unpack(">I", size)[0])
            body_size = header and self._read_exact(4)
            body = body_size and self._read_exact(struct.unpack(">I", body_size)[0])
            if body is None or not header:
                break
            meta = json.loads(header)
            with self._pending_lock:
                slot = self._pending.pop(int(meta["id"]), None)
            if slot is not None:
                slot["result"] = (meta["status"], int(meta["code"]), body)
                slot["done"].set()
        self.alive = False
        with self._pending_lock:
            waiting, self._pending = list(self._pending.values()), {}
        for slot in waiting:
            slot["done"].set()

    def run(
        self, payload: bytes, workdir: str, limits: dict[str, Any], timeout: float
    ) -> tuple[str, int, bytes]:
        if not self.alive or self._process.stdin is None:
            raise _ZygoteUnavailable("zygote stopped")
        request_id = next(self._ids)
        slot: dict[str, Any] = {"done": threading.Event(), "result": None}
        with self._pending_lock:
            self._pending[request_id] = slot
        head = json.dumps(
            {"id": request_id, "workdir": workdir, "timeout": timeout, "limits": limits}
        ).encode()
        try:
            with self._write_lock:
                self._process.stdin.write(
                    struct.pack(">I", len(head)) + head + struct.pack(">I", len(payload)) + payload
                )
                self._process.stdin.flush()
        except OSError as exc:
            self.alive = False
            raise _ZygoteUnavailable("zygote pipe closed") from exc
        if not slot["done"].wait(timeout + 15) or slot["result"] is None:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise _ZygoteUnavailable("no answer from the zygote")
        result: tuple[str, int, bytes] = slot["result"]
        return result


_zygotes: dict[tuple[int, str], _Zygote] = {}
_zygotes_lock = threading.Lock()


def _zygote_for(script: Path) -> _Zygote:
    # Keyed by process id too: a forked server process (prefork workers) starts its own zygote.
    key = (os.getpid(), str(script))
    with _zygotes_lock:
        zygote = _zygotes.get(key)
        if zygote is None or not zygote.alive:
            zygote = _zygotes[key] = _Zygote(script)
        return zygote


def _run_in_child(script: Path, payload: bytes, workdir: str, limits: dict[str, Any]) -> tuple[int, bytes]:
    """(returncode, stdout) of one engine request: a forked zygote child, or a fresh process."""
    from apps.analytics.services import AnalysisError

    timeout = float(settings.ANALYTICS_SANDBOX_TIMEOUT_SECONDS)
    if getattr(settings, "ANALYTICS_SANDBOX_ZYGOTE", False):
        try:
            status, code, stdout = _zygote_for(script).run(payload, workdir, limits, timeout)
        except _ZygoteUnavailable:
            pass  # fall back to a fresh interpreter below
        else:
            if status == "timeout":
                raise AnalysisError("The analysis exceeded its time limit.")
            return code, stdout
    try:
        completed = subprocess.run(  # nosec B603 - fixed interpreter and engine path
            [sys.executable, "-I", str(script)],
            input=payload,
            capture_output=True,
            env=_child_environment(workdir, limits),
            cwd=workdir,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AnalysisError("The analysis exceeded its time limit.") from exc
    return completed.returncode, completed.stdout


def run_engine(
    operation: str, content: bytes, *, script: Path = ENGINE_PATH, **request: Any
) -> dict[str, Any]:
    """Execute one engine operation; raises ``AnalysisError`` or ``SandboxError``.

    ``script`` selects a packaged engine (the analytics or the visualization
    engine); it is always a fixed path inside the application, never input.
    """
    from apps.analytics.services import AnalysisError

    limits = _limits()
    payload = json.dumps(
        {"op": operation, "data": base64.b64encode(content).decode("ascii"), "limits": limits, **request}
    ).encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="jt-analytics-") as workdir:
        returncode, stdout = _run_in_child(script, payload, workdir, limits)
    if returncode != 0 or not stdout:
        if returncode < 0:
            raise AnalysisError("The analysis exceeded its resource limits and was stopped.")
        raise SandboxError(f"Analytics engine exited with status {returncode}.")
    try:
        response = json.loads(stdout)
    except ValueError as exc:
        raise SandboxError("Analytics engine returned an invalid response.") from exc
    if response.get("ok"):
        return dict(response)
    if response.get("kind") in {"analysis", "resources"}:
        raise AnalysisError(str(response.get("error") or "The analysis failed."))
    raise SandboxError(f"Analytics engine failed: {response.get('error')}")
