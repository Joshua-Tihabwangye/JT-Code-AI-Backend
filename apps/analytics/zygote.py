"""Pre-loaded fork server for the analytics/visualization engines (``python -I zygote.py <engine.py>``).

Starting a fresh ``python -I engine.py`` per request spends seconds importing pandas, NumPy, SciPy,
scikit-learn and statsmodels before any work starts. This process imports them once, then forks one
child per request. Each child is exactly what the old per-request process was: it gets its own
private temporary directory and environment, applies the engine's resource limits, installs the
engine's guard (no network, no programs, no files outside its directory), handles one request and
exits. Children never share state with each other; the zygote itself never runs request code.

Protocol on stdin/stdout (both binary, length-prefixed with 4-byte big-endian sizes):
  request  = header JSON {"id", "workdir", "timeout", "limits"} + payload bytes (the engine's stdin)
  response = header JSON {"id", "status": "exit"|"signal"|"timeout", "code"} + child stdout bytes
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import selectors
import signal
import struct
import sys
import tempfile
import time
from typing import Any

PRELOAD = (
    "numpy",
    "pandas",
    "scipy.stats",
    "scipy.optimize",
    "scipy.signal",
    "sklearn.ensemble",
    "sklearn.linear_model",
    "sklearn.model_selection",
    "sklearn.preprocessing",
    "sklearn.cluster",
    "sklearn.metrics",
    "sklearn.inspection",
    "statsmodels.api",
    "statsmodels.tsa.holtwinters",
    "statsmodels.tsa.statespace.sarimax",
    "statsmodels.tsa.seasonal",
    "openpyxl",
)


def _load_engine(path: str) -> Any:
    spec = importlib.util.spec_from_file_location("jt_engine_main", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["jt_engine_main"] = module
    spec.loader.exec_module(module)
    return module


def _preload() -> None:
    for name in PRELOAD:
        try:
            __import__(name)
        except Exception:  # noqa: BLE001, S112 - a missing optional library loads lazily in the child
            continue


def _read_exact(stream: Any, size: int) -> bytes | None:
    chunks: list[bytes] = []
    while size:
        chunk = stream.read(size)
        if not chunk:
            return None
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _write_frame(stream: Any, header: dict[str, Any], body: bytes) -> None:
    head = json.dumps(header).encode("utf-8")
    stream.write(struct.pack(">I", len(head)) + head + struct.pack(">I", len(body)) + body)
    stream.flush()


def _run_child(engine: Any, header: dict[str, Any], payload: bytes, write_fd: int) -> None:
    """In the forked child: become a one-request engine process. Never returns."""
    code = 70
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)  # never read or write the zygote's protocol pipes
        os.dup2(devnull, 1)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        workdir = header["workdir"]
        os.chdir(workdir)
        os.environ.update(
            HOME=workdir,
            TMPDIR=workdir,
            MPLCONFIGDIR=workdir,
            JT_ANALYTICS_LIMITS=json.dumps(header["limits"]),
        )
        tempfile.tempdir = None
        sys.stdin = io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8")
        sys.stdout = io.TextIOWrapper(os.fdopen(write_fd, "wb"), encoding="utf-8")
        code = int(engine.main() or 0)
        sys.stdout.flush()
    except BaseException:  # noqa: BLE001 - the parent sees a non-zero exit status
        code = 70
    os._exit(code)


def serve(engine_path: str) -> int:
    _preload()
    engine = _load_engine(engine_path)
    # Unbuffered: a buffered reader could hold the next request while select() reports nothing to read.
    stdin, stdout = sys.stdin.buffer.raw, sys.stdout.buffer  # type: ignore[union-attr]
    selector = selectors.DefaultSelector()
    children: dict[int, dict[str, Any]] = {}  # read fd -> {id, pid, chunks, deadline, timed_out}
    requests_open = True

    def finish(fd: int) -> None:
        child = children.pop(fd)
        selector.unregister(fd)
        os.close(fd)
        _, status = os.waitpid(child["pid"], 0)
        if child["timed_out"]:
            result, code = "timeout", 0
        elif os.WIFSIGNALED(status):
            result, code = "signal", -os.WTERMSIG(status)
        else:
            result, code = "exit", os.WEXITSTATUS(status)
        _write_frame(stdout, {"id": child["id"], "status": result, "code": code}, b"".join(child["chunks"]))

    selector.register(stdin, selectors.EVENT_READ, "request")
    while requests_open or children:
        now = time.monotonic()
        deadlines = [child["deadline"] for child in children.values() if not child["timed_out"]]
        timeout = max(0.0, min(deadlines) - now) if deadlines else None
        for key, _ in selector.select(timeout):
            if key.data == "request":
                size = _read_exact(stdin, 4)
                header_bytes = size and _read_exact(stdin, struct.unpack(">I", size)[0])
                body_size = header_bytes and _read_exact(stdin, 4)
                payload = body_size and _read_exact(stdin, struct.unpack(">I", body_size)[0])
                if payload is None or not header_bytes:
                    requests_open = False
                    selector.unregister(stdin)
                    continue
                header = json.loads(header_bytes)
                read_fd, write_fd = os.pipe()
                pid = os.fork()
                if pid == 0:
                    os.close(read_fd)
                    for fd in children:
                        os.close(fd)
                    _run_child(engine, header, payload, write_fd)
                os.close(write_fd)
                children[read_fd] = {
                    "id": header["id"],
                    "pid": pid,
                    "chunks": [],
                    "deadline": time.monotonic() + float(header.get("timeout") or 250),
                    "timed_out": False,
                }
                selector.register(read_fd, selectors.EVENT_READ, "child")
            else:
                chunk = os.read(key.fd, 1 << 16)
                if chunk:
                    children[key.fd]["chunks"].append(chunk)
                else:
                    finish(key.fd)
        now = time.monotonic()
        for child in children.values():
            if not child["timed_out"] and child["deadline"] <= now:
                child["timed_out"] = True
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child["pid"], signal.SIGKILL)
    return 0


if __name__ == "__main__":
    raise SystemExit(serve(sys.argv[1]))
