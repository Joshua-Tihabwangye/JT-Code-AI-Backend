"""Runtime guard for the sandboxed visualization engine (PEP 578 audit hooks).

The engine already runs as an isolated ``python -I`` child with an empty environment and resource
limits (``apps/analytics/sandbox.py``). This guard is the next layer: once installed — before the
request is read — it cannot be removed, and it vetoes anything a data-processing job never needs:

* network access (sockets, DNS, HTTP/FTP/SMTP clients);
* starting programs or processes (``subprocess``, ``os.system``, ``exec*``, ``spawn``, ``fork``) and
  signalling others;
* writing, deleting or changing files outside the run's private temporary directory;
* reading files outside the Python installation, the engine's own modules, the temporary directory
  and a few read-only system locations (time zones, shared libraries, CPU information);
* loading native libraries from anywhere else, and unpickling.

It imports nothing from Django or the application, like the other engine modules.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

BLOCKED_EVENTS = frozenset(
    {
        "socket.__new__",
        "socket.connect",
        "socket.bind",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
        "socket.sendto",
        "socket.sendmsg",
        "http.client.connect",
        "urllib.Request",
        "ftplib.connect",
        "smtplib.connect",
        "poplib.connect",
        "imaplib.open",
        "nntplib.connect",
        "telnetlib.Telnet.open",
        "webbrowser.open",
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
        "os.kill",
        "os.killpg",
        "os.startfile",
        "pty.spawn",
        "pickle.find_class",
        "sys.remote_exec",
    }
)
# File-changing events whose path arguments must all stay inside the temporary directory.
MUTATING_EVENTS = frozenset(
    {
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.mkdir",
        "os.chmod",
        "os.chown",
        "os.chflags",
        "os.link",
        "os.symlink",
        "os.truncate",
        "os.utime",
        "os.setxattr",
        "os.removexattr",
        "shutil.copyfile",
        "shutil.copymode",
        "shutil.copystat",
        "shutil.copytree",
        "shutil.move",
        "shutil.rmtree",
        "shutil.chown",
        "shutil.make_archive",
        "shutil.unpack_archive",
    }
)
READ_ONLY_SYSTEM = (
    "/usr/share/zoneinfo",
    "/usr/lib",
    "/usr/lib64",
    "/usr/local/lib",
    "/lib",
    "/lib64",
    "/etc/localtime",
    "/etc/timezone",
    "/etc/mime.types",
    "/proc/self",
    "/proc/cpuinfo",
    "/proc/meminfo",
    "/sys/devices/system/cpu",
    "/sys/fs/cgroup",
    "/dev/null",
    "/dev/urandom",
)
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


class GuardViolation(PermissionError):
    """Raised (inside the engine) when an operation is blocked by the guard."""


class _NoMoreHooks(RuntimeError):
    """Python silently refuses a new audit hook when an existing one raises a RuntimeError."""


def _real(path: Any) -> str | None:
    if isinstance(path, int):
        return None  # an already-open descriptor
    try:
        return os.path.realpath(os.fsdecode(path))
    except TypeError, ValueError:
        return ""


def _within(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def install(temp_dir: str | None = None, engine_dirs: tuple[str, ...] = ()) -> None:
    """Install the guard for the rest of this process's life."""
    temp = os.path.realpath(temp_dir or os.environ.get("TMPDIR") or "/nonexistent-jt-tmp")
    python_roots = tuple(
        {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix)}
    )
    here = os.path.realpath(Path(__file__).resolve().parent)
    code_roots = tuple({here, *(os.path.realpath(d) for d in engine_dirs)})
    write_roots = (temp,)
    read_roots = (*python_roots, *code_roots, temp, *READ_ONLY_SYSTEM)

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event == "sys.addaudithook":
            raise _NoMoreHooks("The engine guard cannot be replaced.")
        if event in BLOCKED_EVENTS:
            raise GuardViolation(f"Blocked by the engine guard: {event}")
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            real = _real(path)
            if real is None:
                return
            writing = (isinstance(mode, str) and any(ch in mode for ch in "wax+")) or (
                isinstance(flags, int) and bool(flags & WRITE_FLAGS)
            )
            if writing and not _within(real, write_roots):
                raise GuardViolation("Blocked by the engine guard: writing outside the sandbox")
            if not writing and not _within(real, read_roots):
                raise GuardViolation("Blocked by the engine guard: reading outside the sandbox")
            return
        if event in MUTATING_EVENTS:
            for value in args:
                if isinstance(value, (str, bytes, os.PathLike)):
                    real = _real(value)
                    if real is not None and not _within(real, write_roots):
                        raise GuardViolation(f"Blocked by the engine guard: {event} outside the sandbox")
            return
        if event == "ctypes.dlopen":
            name = args[0] if args else None
            if isinstance(name, (str, bytes)) and os.sep in os.fsdecode(name):
                real = _real(name)
                if real is not None and not _within(real, (*python_roots, *READ_ONLY_SYSTEM)):
                    raise GuardViolation("Blocked by the engine guard: loading a foreign native library")

    sys.addaudithook(hook)
